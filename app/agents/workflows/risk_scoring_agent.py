"""Risk Scoring Agent — computes risk scores and generates narratives.

Runs third in the pipeline. Uses the entity profiles from Agent 2
and the org's risk configuration to score every entity deterministically,
then uses LLM to generate risk narratives for elevated entities.

Provider: Groq (openai/gpt-oss-120b) for narratives
Rationale: Risk narratives need reasoning depth about signal combinations.
The 120B model handles multi-constraint reasoning significantly better
than the 70B. Scores themselves are deterministic (computed by compute_risk).
"""

import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import BaseAgent, LLMProvider
from app.agents.prompts.risk_scoring import RISK_SCORING_PROMPT
from app.agents.state import PipelineState
from app.infrastructure.database.client_queries import compute_risk, fetch_entities, get_schema_mapping
from app.infrastructure.external_services.rag import (
    enrich_entities_with_similar,
    update_entity_metadata,
)
from app.config.settings import settings

logger = logging.getLogger(__name__)


class RiskScoringAgent(BaseAgent):
    """Scores entities using deterministic risk model + LLM narratives.

    Uses deterministic compute_risk() for scores.
    Uses Groq GPT-OSS-120B for risk narrative generation.
    """

    def __init__(self) -> None:
        super().__init__(
            name="RiskScoringAgent",
            provider=LLMProvider.GROQ,
            default_model=settings.GROQ_LLM_MODEL_HEAVY,
        )

    async def run(
        self, state: PipelineState, db: AsyncSession
    ) -> PipelineState:
        """Execute risk scoring with ML predictions or deterministic fallback."""

        org_id = UUID(state["org_id"])
        use_ml = state.get("ml_available") and state.get("ml_scored_entities")

        if use_ml:
            # ── ML-first path: use predictions from Model Training Agent ──
            logger.info("[RiskScoringAgent] Using ML-predicted risk scores")
            ml_scored = state["ml_scored_entities"]

            # ── Validate ML scores before using them ──
            invalid_scores = [
                e for e in ml_scored
                if not isinstance(e.get("risk_score"), (int, float))
                or e["risk_score"] < 0.0 or e["risk_score"] > 1.0
            ]
            if invalid_scores:
                logger.warning(
                    "[RiskScoringAgent] %d ML scores outside [0,1] range — "
                    "falling back to rule-based scoring",
                    len(invalid_scores),
                )
                use_ml = False

        if use_ml:
            ml_scored = state["ml_scored_entities"]

            # Fetch entity names and signal values for display/narratives
            try:
                mapping = await get_schema_mapping(db, org_id)
                entities = await fetch_entities(db, org_id, mapping)
                id_col = mapping.entity_id_col
                name_col = mapping.entity_name_col
                name_lookup = {
                    str(e[id_col]): str(e.get(name_col)) if name_col and e.get(name_col) else None
                    for e in entities
                }
                signal_lookup = {}
                for e in entities:
                    eid = str(e[id_col])
                    signal_lookup[eid] = {
                        sig_label: e.get(col_name)
                        for sig_label, col_name in (mapping.signal_columns or {}).items()
                        if col_name in e
                    }
            except Exception as e:
                logger.warning("[RiskScoringAgent] Failed to fetch entity names: %s", e)
                name_lookup = {}
                signal_lookup = {}

            # ── Validate entity coverage ──
            if entities:
                coverage = len(ml_scored) / len(entities)
                if coverage < 0.5:
                    logger.warning(
                        "[RiskScoringAgent] ML scored only %d of %d entities (%.1f%%) — "
                        "falling back to rule-based scoring for better coverage",
                        len(ml_scored), len(entities), coverage * 100,
                    )
                    use_ml = False

        if use_ml:
            scored_entities = []
            for ml_entity in ml_scored:
                eid = str(ml_entity["entity_id"])
                score = float(ml_entity["risk_score"])
                # Clamp score and RE-DERIVE tier (single source of truth)
                score = max(0.0, min(1.0, score))

                if score >= 0.8:
                    tier = "critical"
                elif score >= 0.6:
                    tier = "high"
                elif score >= 0.4:
                    tier = "medium"
                else:
                    tier = "low"

                scored_entities.append({
                    "entity_id": eid,
                    "entity_name": name_lookup.get(eid),
                    "risk_score": round(score, 4),
                    "risk_tier": tier,
                    "signal_values": signal_lookup.get(eid, {}),
                    "risk_narrative": None,
                    "scoring_method": "ml",
                })
        else:
            # ── Deterministic fallback: use compute_risk() ──
            if state.get("ml_available"):
                logger.info("[RiskScoringAgent] ML validation failed — falling back to rule-based scoring")
            else:
                logger.info("[RiskScoringAgent] Using deterministic rule-based scoring")

            try:
                mapping = await get_schema_mapping(db, org_id)
                entities = await fetch_entities(db, org_id, mapping)
                scored = compute_risk(entities, mapping.signal_columns, mapping.risk_config)
            except Exception as e:
                logger.error("[RiskScoringAgent] Failed to compute risk: %s", e)
                state["scored_entities"] = []
                state["risk_summary"] = {"error": str(e)}
                state["error"] = f"Risk scoring failed: {e}"
                state["reasoning_log"].extend(self._reasoning_entries)
                return state

            id_col = mapping.entity_id_col
            name_col = mapping.entity_name_col
            scored_entities = []
            for entity in scored:
                scored_entities.append({
                    "entity_id": str(entity[id_col]),
                    "entity_name": str(entity.get(name_col)) if name_col and entity.get(name_col) else None,
                    "risk_score": entity["risk_score"],
                    "risk_tier": entity["risk_tier"],
                    "signal_values": entity.get("signals", {}),
                    "risk_narrative": None,
                    "scoring_method": "rule_based",
                })

        # Sort by risk score descending
        scored_entities.sort(key=lambda e: e["risk_score"], reverse=True)

        # Generate LLM narratives for elevated entities (risk_score >= 0.6)
        elevated = [e for e in scored_entities if e["risk_score"] >= 0.6]

        narrative_cap = 50
        narratives_target = elevated[:narrative_cap]

        if len(elevated) > narrative_cap:
            caps = dict(state.get("generation_caps") or {})
            caps["narratives"] = {
                "elevated_total": len(elevated),
                "limit": narrative_cap,
                "truncated": True,
            }
            state["generation_caps"] = caps

        # Pull per-entity profiles from Agent 2 so narratives can reason over
        # behavioural context, not just deterministic signal values.
        profile_index = {
            str(p.get("entity_id")): p
            for p in (state.get("entity_profiles") or [])
            if p.get("entity_id") is not None
        }

        if narratives_target:
            payload = [
                _augment_with_profile(e, profile_index.get(e["entity_id"]))
                for e in narratives_target
            ]

            # When ML is active, enrich narrative context with feature importances
            if use_ml and state.get("feature_importances"):
                for p in payload:
                    p["ml_feature_importances"] = state["feature_importances"][:10]
                    p["scoring_method"] = "ml"

            # RAG: attach similar past-profile entities so narratives can reference
            # precedent. Helper degrades gracefully when Voyage/Qdrant is unavailable.
            payload = await enrich_entities_with_similar(str(org_id), payload)

            try:
                narratives = await self._generate_narratives(state, payload)
                narrative_map = {n["entity_id"]: n.get("risk_narrative", "") for n in narratives}
                for entity in scored_entities:
                    if entity["entity_id"] in narrative_map:
                        entity["risk_narrative"] = narrative_map[entity["entity_id"]]
            except Exception as e:
                logger.warning("[RiskScoringAgent] Narrative generation failed (non-fatal): %s", e)
                # Non-fatal — scores are still valid without narratives

        # Build risk summary
        tier_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for entity in scored_entities:
            tier_counts[entity["risk_tier"]] += 1

        # Find most common risk drivers across elevated entities
        if use_ml and state.get("feature_importances"):
            top_signals = [fi["feature"] for fi in state["feature_importances"][:5]]
        else:
            signal_freq: dict[str, int] = {}
            for entity in elevated:
                for signal, value in entity.get("signal_values", {}).items():
                    if isinstance(value, (int, float)) and value > 0:
                        signal_freq[signal] = signal_freq.get(signal, 0) + 1
            top_signals = sorted(signal_freq, key=lambda k: signal_freq[k], reverse=True)[:5]

        scoring_method = "ml" if use_ml else "rule_based"
        model_accuracy = state.get("model_metrics", {}).get("accuracy")

        state["scored_entities"] = scored_entities
        state["risk_summary"] = {
            "total_scored": len(scored_entities),
            "critical_count": tier_counts["critical"],
            "high_count": tier_counts["high"],
            "medium_count": tier_counts["medium"],
            "low_count": tier_counts["low"],
            "top_risk_signals": top_signals,
            "scoring_method": scoring_method,
            "model_accuracy": model_accuracy,
            "key_findings": (
                f"{tier_counts['critical']} critical and {tier_counts['high']} high-risk "
                f"{state.get('entity_label', 'entities')} identified"
                f"{f' using ML model (accuracy: {model_accuracy:.1%})' if model_accuracy else ' using rule-based scoring'}. "
                f"Top risk drivers: {', '.join(top_signals[:3]) if top_signals else 'N/A'}."
            ),
        }
        state["reasoning_log"].extend(self._reasoning_entries)

        # Patch Qdrant payloads with risk_tier + last_scored_at so subsequent
        # cycles can filter retrieval by tier and freshness. Non-fatal on error.
        import time as _time
        _scored_ts = _time.time()
        await update_entity_metadata(
            str(org_id),
            [
                (
                    e["entity_id"],
                    {
                        "risk_tier": e["risk_tier"],
                        "risk_score": e["risk_score"],
                        "last_scored_at": _scored_ts,
                    },
                )
                for e in scored_entities
            ],
        )

        logger.info(
            "[RiskScoringAgent] Complete (%s): %d scored, %d critical, %d high",
            scoring_method, len(scored_entities), tier_counts["critical"], tier_counts["high"],
        )
        return state

    async def _generate_narratives(
        self, state: PipelineState, elevated: list[dict]
    ) -> list[dict]:
        """Use GPT-OSS-120B on Groq to generate risk narratives for elevated entities."""

        prompt = RISK_SCORING_PROMPT.format(
            org_name=state.get("org_name", "Unknown"),
            industry=state.get("industry", "Unknown"),
            goal_label=state.get("goal_label", "improve operations"),
            entity_label=state.get("entity_label", "entities"),
            signal_columns=json.dumps(state.get("signal_columns", {})),
            risk_config=json.dumps(state.get("risk_config", {})),
        )

        entity_data = json.dumps(elevated, default=str)
        user_prompt = (
            f"Generate risk narratives for these {len(elevated)} elevated-risk "
            f"{state.get('entity_label', 'entities')}. "
            f"Each entity already has a deterministic risk_score and risk_tier. "
            f"Your job is to write a 1-2 sentence risk_narrative for each one "
            f"explaining WHY their specific signal values make them a priority.\n\n"
            f"Entities:\n{entity_data}"
        )

        raw = await self.llm_json_call(
            system_prompt=prompt,
            user_prompt=user_prompt,
        )

        try:
            result = json.loads(raw)
            if isinstance(result, dict) and "scored_entities" in result:
                return result["scored_entities"]
            if isinstance(result, list):
                return result
            return []
        except json.JSONDecodeError:
            logger.warning("[RiskScoringAgent] Failed to parse narratives JSON")
            return []


def _augment_with_profile(entity: dict, profile: dict | None) -> dict:
    """Merge selected profiling fields into an entity payload for the LLM.

    The profile is only attached for in-memory narrative generation. Profile
    data is never persisted to the Pulse application database.
    """
    if not profile:
        return entity
    enriched = dict(entity)
    profile_fields = {
        k: v
        for k, v in profile.items()
        if k not in {"entity_id", "entity_name", "risk_score", "risk_tier", "signals"}
    }
    if profile_fields:
        enriched["profile"] = profile_fields
    return enriched
