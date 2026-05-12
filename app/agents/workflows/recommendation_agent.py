"""Recommendation Agent — generates personalised recommendations.

Runs last in the pipeline. Takes scored entities with risk >= 0.6 and
generates actionable, entity-specific recommendations using LLM.
Writes results to Pulse's own recommendations table.

Provider: Groq (openai/gpt-oss-120b)
Rationale: Recommendation quality is directly user-facing. The 120B model produces substantially better instruction following on multi-constraint tasks like reasoning about
specific signal combinations to generate tailored interventions.
"""

import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import BaseAgent, LLMProvider
from app.agents.prompts.recommendation import RECOMMENDATION_PROMPT
from app.agents.state import PipelineState
from app.infrastructure.database.repositories.recommendation_repository import (
    RecommendationRepository,
)
from app.config.settings import settings

logger = logging.getLogger(__name__)

DEFAULT_RECOMMENDATION_LIMIT = 50


class RecommendationAgent(BaseAgent):
    """Generates personalised, actionable recommendations for at-risk entities.

    Uses Groq GPT-OSS-120B for maximum reasoning quality on user-facing output.
    Falls back to template-based recommendations if LLM fails.
    """

    def __init__(self) -> None:
        super().__init__(
            name="RecommendationAgent",
            provider=LLMProvider.GROQ,
            default_model=settings.GROQ_LLM_MODEL_HEAVY,
        )

    async def run(
        self, state: PipelineState, db: AsyncSession
    ) -> PipelineState:
        """Generate recommendations for elevated-risk entities."""

        org_id = UUID(state["org_id"])
        scored = state.get("scored_entities", [])
        recommendation_limit = DEFAULT_RECOMMENDATION_LIMIT

        # Filter to entities with risk_score >= 0.6
        at_risk = [e for e in scored if e.get("risk_score", 0) >= 0.6][:recommendation_limit]

        if not at_risk:
            logger.info("[RecommendationAgent] No at-risk entities to recommend for")
            state["recommendations"] = []
            state["recommendation_stats"] = {"total_generated": 0}
            return state

        # Generate recommendations via LLM
        prompt = RECOMMENDATION_PROMPT.format(
            org_name=state.get("org_name", "Unknown"),
            industry=state.get("industry", "Unknown"),
            business_context=state.get("business_context", ""),
            entity_label=state.get("entity_label", "entities"),
            goal_label=state.get("goal_label", "improve operations"),
            recommendation_limit=recommendation_limit,
        )

        # Batch entities for LLM processing (max ~20 per call to keep context manageable)
        all_recs: list[dict] = []
        batch_size = 20

        for i in range(0, len(at_risk), batch_size):
            batch = at_risk[i : i + batch_size]
            try:
                batch_recs = await self._generate_batch(prompt, state, batch)
                all_recs.extend(batch_recs)
            except Exception as e:
                logger.error(
                    "[RecommendationAgent] Batch %d failed: %s", i // batch_size, e
                )
                # Fall back to template-based recommendations for this batch
                all_recs.extend(self._fallback_recommendations(batch, state))

        # Persist to database — supersede existing active recs
        try:
            repo = RecommendationRepository(db)
            existing = await repo.list_by_org(org_id, status="active")
            for rec in existing:
                rec.status = "superseded"

            created = 0
            for rec_data in all_recs:
                await repo.create(
                    org_id=org_id,
                    entity_id=str(rec_data.get("entity_id", "")),
                    entity_label=rec_data.get("entity_name"),
                    type=rec_data.get("type", "retention_intervention"),
                    urgency=rec_data.get("urgency", "high"),
                    title=rec_data.get("title", "Risk intervention required"),
                    reasoning=rec_data.get("reasoning", ""),
                    suggested_action=rec_data.get("suggested_action", ""),
                    status="active",
                )
                created += 1

            logger.info(
                "[RecommendationAgent] Persisted: %d new recs, %d superseded",
                created, len(existing),
            )
        except Exception as e:
            logger.error("[RecommendationAgent] DB persistence failed: %s", e)
            state["error"] = f"Recommendation persistence failed: {e}"

        # Build stats
        by_urgency: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for rec in all_recs:
            urg = rec.get("urgency", "high")
            by_urgency[urg] = by_urgency.get(urg, 0) + 1
            rtype = rec.get("type", "other")
            by_type[rtype] = by_type.get(rtype, 0) + 1

        state["recommendations"] = all_recs
        state["recommendation_stats"] = {
            "total_generated": len(all_recs),
            "by_urgency": by_urgency,
            "by_type": by_type,
        }
        state["reasoning_log"].extend(self._reasoning_entries)

        logger.info(
            "[RecommendationAgent] Complete: %d recommendations generated",
            len(all_recs),
        )
        return state

    async def _generate_batch(
        self,
        system_prompt: str,
        state: PipelineState,
        batch: list[dict],
    ) -> list[dict]:
        """Generate recommendations for a batch of entities via GPT-OSS-120B."""

        entity_data = json.dumps(batch, default=str)
        user_prompt = (
            f"Generate personalised recommendations for these {len(batch)} "
            f"at-risk {state.get('entity_label', 'entities')}. "
            f"Each one has specific signal values driving their risk — "
            f"reference those values in your reasoning and suggested actions.\n\n"
            f"Entities:\n{entity_data}"
        )

        raw = await self.llm_json_call(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )

        result = json.loads(raw)
        if isinstance(result, dict) and "recommendations" in result:
            return result["recommendations"]
        if isinstance(result, list):
            return result
        return []

    @staticmethod
    def _fallback_recommendations(
        batch: list[dict], state: PipelineState
    ) -> list[dict]:
        """Generate template-based recommendations as fallback when LLM fails."""
        recs = []
        for entity in batch:
            tier = entity.get("risk_tier", "high")
            signals = entity.get("signal_values", {})
            top_signal = max(signals, key=lambda k: signals[k]) if signals else "unknown"

            recs.append({
                "entity_id": entity.get("entity_id", ""),
                "entity_name": entity.get("entity_name"),
                "risk_score": entity.get("risk_score", 0),
                "risk_tier": tier,
                "type": "retention_intervention",
                "urgency": "critical" if tier == "critical" else "high",
                "title": f"{tier.title()} risk — intervention required",
                "reasoning": (
                    f"Risk score of {entity.get('risk_score', 0):.2f} ({tier} tier). "
                    f"Primary risk driver: {top_signal}."
                ),
                "suggested_action": (
                    f"Review this {state.get('entity_label', 'entity')}'s profile "
                    f"and take appropriate {state.get('goal_label', 'retention')} action."
                ),
            })
        return recs
