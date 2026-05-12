"""Pipeline Orchestrator — sequences the four autonomous agents.

Production-grade orchestration with:
- Per-step retry with configurable attempts
- Step-level timing and metrics collection
- Graceful degradation (schema errors don't block downstream)
- Full pipeline run summary for observability
- Agent instance isolation (fresh metrics per run)
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.state import PipelineState
from app.agents.workflows.schema_intelligence_agent import SchemaIntelligenceAgent
from app.agents.workflows.profiling_agent import ProfilingAgent
from app.agents.workflows.risk_scoring_agent import RiskScoringAgent
from app.agents.workflows.recommendation_agent import RecommendationAgent
from app.infrastructure.database.client_queries import get_schema_mapping
from app.infrastructure.database.repositories.organization_repository import (
    OrganizationRepository,
)

logger = logging.getLogger(__name__)

# Steps that can fail without aborting the entire pipeline
_NON_FATAL_STEPS = {"schema_intelligence"}

# Max retry attempts per step
_STEP_MAX_RETRIES = 2


class PipelineOrchestrator:
    """Executes the full autonomous agent pipeline for one organisation.

    Each execution creates fresh agent instances to ensure clean metrics
    and no state leakage between runs.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def execute(self, org_id: UUID) -> PipelineState:
        """Run the complete pipeline for one organisation."""

        state = await self._build_initial_state(org_id)
        pipeline_start = time.monotonic()
        step_metrics: list[dict] = []

        # Fresh agent instances per run — no shared state
        pipeline = [
            ("schema_intelligence", SchemaIntelligenceAgent()),
            ("profiling", ProfilingAgent()),
            ("risk_scoring", RiskScoringAgent()),
            ("recommendation", RecommendationAgent()),
        ]

        logger.info(
            "═══ Pipeline started for org '%s' (%s) ═══",
            state.get("org_name", "unknown"), org_id,
        )

        for step_name, agent in pipeline:
            state["current_step"] = step_name
            step_start = time.monotonic()
            step_error = None

            # Retry loop per step
            for attempt in range(1, _STEP_MAX_RETRIES + 1):
                try:
                    agent.reset_metrics()
                    state = await agent.run(state, self._session)
                    step_error = state.get("error")

                    if step_error and attempt < _STEP_MAX_RETRIES:
                        logger.warning(
                            "[Pipeline] Step '%s' attempt %d failed: %s — retrying",
                            step_name, attempt, step_error,
                        )
                        state["error"] = None
                        continue
                    break  # Success or final attempt

                except Exception as e:
                    step_error = str(e)
                    if attempt < _STEP_MAX_RETRIES:
                        logger.warning(
                            "[Pipeline] Step '%s' attempt %d raised: %s — retrying",
                            step_name, attempt, e,
                        )
                        continue
                    logger.error(
                        "[Pipeline] Step '%s' failed after %d attempts: %s",
                        step_name, _STEP_MAX_RETRIES, e,
                    )
                    state["error"] = f"{step_name} failed: {e}"
                    break

            step_elapsed = int((time.monotonic() - step_start) * 1000)

            # Collect agent metrics for this step
            agent_metrics = agent.get_metrics_summary()
            step_metrics.append({
                "step": step_name,
                "duration_ms": step_elapsed,
                "success": step_error is None,
                "error": step_error,
                **agent_metrics,
            })

            if step_error:
                if step_name in _NON_FATAL_STEPS:
                    logger.warning(
                        "[Pipeline] Non-fatal step '%s' failed (%dms) — continuing",
                        step_name, step_elapsed,
                    )
                    state["error"] = None  # Clear so downstream can proceed
                else:
                    logger.error(
                        "[Pipeline] Fatal step '%s' failed (%dms) — aborting",
                        step_name, step_elapsed,
                    )
                    break
            else:
                logger.info(
                    "[Pipeline] ✓ Step '%s' completed in %dms "
                    "(llm_calls=%d, tool_calls=%d, tokens=%d)",
                    step_name, step_elapsed,
                    agent_metrics.get("llm_calls", 0),
                    agent_metrics.get("tool_calls", 0),
                    agent_metrics.get("total_tokens", 0),
                )

        total_ms = int((time.monotonic() - pipeline_start) * 1000)
        state["completed_at"] = datetime.now(timezone.utc).isoformat()
        state["current_step"] = (
            "completed" if not state.get("error") else state.get("current_step", "failed")
        )

        # Commit any pending DB changes (recommendations, etc.)
        try:
            await self._session.commit()
        except Exception as e:
            logger.error("[Pipeline] Final commit failed: %s", e)
            state["error"] = f"Commit failed: {e}"

        # Aggregate pipeline-level metrics
        total_tokens = sum(s.get("total_tokens", 0) for s in step_metrics)
        total_llm = sum(s.get("llm_calls", 0) for s in step_metrics)
        total_tools = sum(s.get("tool_calls", 0) for s in step_metrics)
        total_fallbacks = sum(s.get("provider_fallbacks", 0) for s in step_metrics)
        all_providers = set()
        for s in step_metrics:
            all_providers.update(s.get("providers_used", []))

        risk_summary = state.get("risk_summary", {})
        rec_stats = state.get("recommendation_stats", {})

        logger.info(
            "═══ Pipeline complete for org '%s' in %dms ═══\n"
            "  Entities scored: %d | Critical: %d | High: %d\n"
            "  Recommendations: %d | LLM calls: %d | Tool calls: %d\n"
            "  Tokens: %d | Providers: %s | Fallbacks: %d",
            state.get("org_name", "unknown"), total_ms,
            risk_summary.get("total_scored", 0),
            risk_summary.get("critical_count", 0),
            risk_summary.get("high_count", 0),
            rec_stats.get("total_generated", 0),
            total_llm, total_tools, total_tokens,
            ", ".join(sorted(all_providers)) or "none",
            total_fallbacks,
        )

        # Attach pipeline metrics to state for downstream consumers
        state["pipeline_metrics"] = {
            "total_duration_ms": total_ms,
            "total_llm_calls": total_llm,
            "total_tool_calls": total_tools,
            "total_tokens": total_tokens,
            "provider_fallbacks": total_fallbacks,
            "providers_used": sorted(all_providers),
            "steps": step_metrics,
        }

        return state

    async def _build_initial_state(self, org_id: UUID) -> PipelineState:
        """Build the initial state from the org's configuration in Pulse DB."""

        org_repo = OrganizationRepository(self._session)
        org = await org_repo.get_by_id(org_id)
        if not org:
            raise ValueError(f"Organisation {org_id} not found")

        mapping = await get_schema_mapping(self._session, org_id)

        state: PipelineState = {
            "org_id": str(org_id),
            "org_name": org.name,
            "entity_label": org.entity_label or "entities",
            "goal_label": org.goal_label or "improve operations",
            "business_context": org.business_context or "",
            "industry": org.industry or "Unknown",
            "connection_id": str(mapping.connection_id),
            "entity_table": mapping.entity_table or "",
            "entity_id_col": mapping.entity_id_col or "",
            "entity_name_col": mapping.entity_name_col,
            "signal_columns": mapping.signal_columns or {},
            "timestamp_col": mapping.timestamp_col,
            "risk_config": mapping.risk_config or {},
            "raw_schema": mapping.raw_schema or {},
            # Populated by agents
            "schema_analysis": {},
            "validated_columns": [],
            "related_tables": [],
            "schema_issues": [],
            "entity_profiles": [],
            "profile_stats": {},
            "scored_entities": [],
            "risk_summary": {},
            "recommendations": [],
            "recommendation_stats": {},
            # Control
            "current_step": "initializing",
            "error": None,
            "reasoning_log": [],
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
        }

        return state
