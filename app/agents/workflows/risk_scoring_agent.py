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
        """Execute risk scoring with deterministic scores and LLM narratives."""

        org_id = UUID(state["org_id"])

        # Step 1: Get deterministic risk scores from the existing compute_risk engine
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

        # Step 2: Build scored entity list with signal values
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
            })

        # Step 3: Sort by risk score descending
        scored_entities.sort(key=lambda e: e["risk_score"], reverse=True)

        # Step 4: Generate LLM narratives for elevated entities (risk_score >= 0.6)
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
            try:
                narratives = await self._generate_narratives(state, payload)
                narrative_map = {n["entity_id"]: n.get("risk_narrative", "") for n in narratives}
                for entity in scored_entities:
                    if entity["entity_id"] in narrative_map:
                        entity["risk_narrative"] = narrative_map[entity["entity_id"]]
            except Exception as e:
                logger.warning("[RiskScoringAgent] Narrative generation failed (non-fatal): %s", e)
                # Non-fatal — scores are still valid without narratives

        # Step 5: Build risk summary
        tier_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for entity in scored_entities:
            tier_counts[entity["risk_tier"]] += 1

        # Find most common risk drivers across elevated entities
        signal_freq: dict[str, int] = {}
        for entity in elevated:
            for signal, value in entity.get("signal_values", {}).items():
                if isinstance(value, (int, float)) and value > 0:
                    signal_freq[signal] = signal_freq.get(signal, 0) + 1
        top_signals = sorted(signal_freq, key=lambda k: signal_freq[k], reverse=True)[:5]

        state["scored_entities"] = scored_entities
        state["risk_summary"] = {
            "total_scored": len(scored_entities),
            "critical_count": tier_counts["critical"],
            "high_count": tier_counts["high"],
            "medium_count": tier_counts["medium"],
            "low_count": tier_counts["low"],
            "top_risk_signals": top_signals,
            "key_findings": (
                f"{tier_counts['critical']} critical and {tier_counts['high']} high-risk "
                f"{state.get('entity_label', 'entities')} identified. "
                f"Top risk drivers: {', '.join(top_signals[:3]) if top_signals else 'N/A'}."
            ),
        }
        state["reasoning_log"].extend(self._reasoning_entries)

        logger.info(
            "[RiskScoringAgent] Complete: %d scored, %d critical, %d high",
            len(scored_entities), tier_counts["critical"], tier_counts["high"],
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
