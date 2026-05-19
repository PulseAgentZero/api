"""Model Training Agent — trains ML model on client data for risk prediction.

Runs third in the pipeline (between Profiling and Risk Scoring). Uses
entity data + discovered/mapped target variable to train a Random Forest
classifier via scikit-learn tools. If successful, downstream Risk Scoring
Agent uses ML predictions; if not, it falls back to rule-based scoring.

Provider: Groq (openai/gpt-oss-120b)
Rationale: This agent is the gateway for the entire ML scoring path.
A wrong target variable selection or bad quality judgment means all
downstream risk scores are wrong. Requires the same reasoning depth
as Risk Scoring and Recommendation agents — complex multi-step tool
orchestration with data reasoning and quality interpretation. The ~2s
extra latency vs 70B is negligible in a pipeline that runs minutes.

Production hardening:
- Post-LLM validation of scored entities (count, score ranges, IDs)
- Entity coverage sanity check
- Graceful degradation on all failure modes
"""

import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import BaseAgent, LLMProvider
from app.agents.prompts.model_training import MODEL_TRAINING_PROMPT
from app.agents.state import PipelineState
from app.agents.tools.ml_tools import build_ml_tools, _clean_model_store, _SCORED_STORE
from app.agents.tools.query_tools import build_query_tools
from app.config.settings import settings
from app.services.procedural_memory import format_procedural_block

logger = logging.getLogger(__name__)

# Minimum % of expected entities that must be scored for ML to be considered valid
_MIN_COVERAGE_PCT = 0.5


class ModelTrainingAgent(BaseAgent):
    """Trains an ML model on the org's data and scores all entities.

    Uses Groq GPT-OSS-120B for tool orchestration — the same reasoning
    tier as Risk Scoring and Recommendation agents. Falls back gracefully
    if ML isn't possible (sets ml_available=False, pipeline continues
    with rule-based scoring).
    """

    def __init__(self) -> None:
        super().__init__(
            name="ModelTrainingAgent",
            provider=LLMProvider.GROQ,
            default_model=settings.GROQ_LLM_MODEL_HEAVY,
        )

    async def run(
        self, state: PipelineState, db: AsyncSession
    ) -> PipelineState:
        """Execute ML model training and entity scoring."""

        org_id = UUID(state["org_id"])

        # Initialise ML state defaults
        state["ml_available"] = False
        state["model_metrics"] = {}
        state["feature_importances"] = []
        state["ml_scored_entities"] = []

        # Register ML tools + query tools (agent needs to fetch data)
        self.registry = type(self.registry)()
        for tool in build_ml_tools(db, org_id):
            self.registry.register(tool)
        for tool in build_query_tools(db, org_id):
            self.registry.register(tool)

        # Build the target column hint from state
        target_hint = state.get("target_column") or "not provided — auto-discover"

        prompt = MODEL_TRAINING_PROMPT.format(
            org_name=state.get("org_name", "Unknown"),
            industry=state.get("industry", "Unknown"),
            business_context=state.get("business_context", ""),
            entity_label=state.get("entity_label", "entities"),
            goal_label=state.get("goal_label", "improve operations"),
            entity_table=state.get("entity_table", ""),
            entity_id_col=state.get("entity_id_col", ""),
            entity_name_col=state.get("entity_name_col", ""),
            related_tables=json.dumps(state.get("related_tables", []), default=str),
            signal_columns=json.dumps(state.get("signal_columns", {})),
            target_column_hint=target_hint,
            procedural_block=format_procedural_block(
                state.get("procedural_learnings")
            ),
        )

        user_prompt = (
            f"Train an ML model to predict risk for {state.get('entity_label', 'entities')} "
            f"in the '{state.get('entity_table', '')}' table. "
            f"The business goal is: {state.get('goal_label', 'improve operations')}. "
            f"Target column hint: {target_hint}. "
            f"Follow your step-by-step process: identify target → gather data → "
            f"prepare features → train model → score entities. "
            f"Remember: ALWAYS exclude '{state.get('entity_id_col', '')}' and "
            f"'{state.get('entity_name_col', '')}' from features."
        )

        try:
            raw = await self.reason_and_act_json(
                system_prompt=prompt,
                user_prompt=user_prompt,
                required_keys=["ml_available"],
                max_iterations=15,
                max_tokens=8192,
            )
            result = json.loads(raw)
        except Exception as e:
            logger.error("[ModelTrainingAgent] Failed: %s", e)
            state["model_metrics"] = {"error": str(e)}
            state["reasoning_log"].extend(self._reasoning_entries)
            _clean_model_store()
            return state

        # ── Extract and VALIDATE results ──
        ml_available = result.get("ml_available", False)

        if ml_available:
            # Scored entities are stored in-memory by score_entities tool as the LLM cannot fit 7000+ entities in its JSON output.
            scored_entities: list[dict] = []
            data_id = result.get("data_id", "")
            # Try the LLM-reported data_id first, then fall back to any non-empty entry in _SCORED_STORE (handles LLM placeholder copying).
            if data_id and not data_id.startswith("the "):
                scored_entities = _SCORED_STORE.get(data_id, [])
            if not scored_entities and _SCORED_STORE:
                # Take the entry with the most scored entities
                scored_entities = max(_SCORED_STORE.values(), key=len, default=[])
            model_metrics = result.get("model_metrics", {})
            feature_importances = result.get("feature_importances", [])

            # Validation 1: scored entities must be a non-empty list
            if not isinstance(scored_entities, list) or not scored_entities:
                logger.warning(
                    "[ModelTrainingAgent] ml_available=True but no scored entities — "
                    "falling back to rule-based scoring"
                )
                ml_available = False

            # Validation 2: all scores must be in [0.0, 1.0]
            if ml_available:
                invalid_scores = [
                    e for e in scored_entities
                    if not isinstance(e.get("risk_score"), (int, float))
                    or e["risk_score"] < 0.0 or e["risk_score"] > 1.0
                ]
                if invalid_scores:
                    logger.warning(
                        "[ModelTrainingAgent] %d entities have invalid scores (outside [0,1]) — "
                        "falling back to rule-based scoring",
                        len(invalid_scores),
                    )
                    ml_available = False

            # Validation 3: all entities must have entity_id as string
            if ml_available:
                bad_ids = [e for e in scored_entities if not isinstance(e.get("entity_id"), str)]
                if bad_ids:
                    logger.warning(
                        "[ModelTrainingAgent] %d entities have non-string IDs — "
                        "falling back to rule-based scoring",
                        len(bad_ids),
                    )
                    ml_available = False

            # Validation 4: entity coverage sanity check
            if ml_available:
                expected_count = len(state.get("entity_profiles", []))
                if expected_count > 0:
                    coverage = len(scored_entities) / expected_count
                    if coverage < _MIN_COVERAGE_PCT:
                        logger.warning(
                            "[ModelTrainingAgent] Low entity coverage: scored %d of %d expected "
                            "(%.1f%%) — below %.0f%% threshold. Proceeding with caution.",
                            len(scored_entities), expected_count,
                            coverage * 100, _MIN_COVERAGE_PCT * 100,
                        )
                        # Don't fail here — partial scoring is still useful
                        # but log it for observability

            # Validation 5: accuracy must be reasonable
            if ml_available:
                accuracy = model_metrics.get("accuracy", 0)
                if isinstance(accuracy, (int, float)) and accuracy < 0.55:
                    logger.warning(
                        "[ModelTrainingAgent] LLM reported ml_available=True but accuracy "
                        "%.4f < 0.55 threshold — overriding to rule-based scoring",
                        accuracy,
                    )
                    ml_available = False

        state["ml_available"] = ml_available

        if ml_available:
            state["target_column"] = result.get("target_column")
            state["model_metrics"] = result.get("model_metrics", {})
            state["feature_importances"] = result.get("feature_importances", [])
            state["ml_scored_entities"] = scored_entities

            logger.info(
                "[ModelTrainingAgent] ✅ ML model trained: accuracy=%.4f, "
                "scored=%d entities, top_feature=%s",
                state["model_metrics"].get("accuracy", 0),
                len(state["ml_scored_entities"]),
                state["feature_importances"][0]["feature"] if state["feature_importances"] else "N/A",
            )
        else:
            reason = result.get("reason", "Validation failed (see logs)")
            logger.info(
                "[ModelTrainingAgent] ML not available: %s — pipeline will use rule-based scoring",
                reason,
            )
            state["model_metrics"] = {
                "ml_unavailable_reason": reason,
                "training_summary": result.get("training_summary", ""),
            }

        state["reasoning_log"].extend(self._reasoning_entries)
        _clean_model_store()
        return state
