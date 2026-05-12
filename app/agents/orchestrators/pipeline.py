"""Pipeline Orchestrator — sequences the four autonomous agents.

Production-grade orchestration with:
- Durable run records (pipeline_runs) for audit history and dedup
- Client DB preflight to fail fast on unreachable client databases
- Per-step retry with configurable attempts
- Per-step timeout via asyncio.wait_for
- Step-level timing and metrics collection
- Graceful degradation (schema errors don't block downstream)
- Agent instance isolation (fresh metrics per run)
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.agents.state import PipelineState
from app.agents.workflows.schema_intelligence_agent import SchemaIntelligenceAgent
from app.agents.workflows.profiling_agent import ProfilingAgent
from app.agents.workflows.risk_scoring_agent import RiskScoringAgent
from app.agents.workflows.recommendation_agent import RecommendationAgent
from app.infrastructure.crypto import decrypt_dsn
from app.infrastructure.database.client_queries import get_schema_mapping
from app.infrastructure.database.models.connection import Connection
from app.infrastructure.database.models.pipeline_run import PipelineRun
from app.infrastructure.database.repositories.organization_repository import (
    OrganizationRepository,
)
from app.infrastructure.database.repositories.pipeline_run_repository import (
    PipelineRunRepository,
)

logger = logging.getLogger(__name__)

# Steps that can fail without aborting the entire pipeline
_NON_FATAL_STEPS = {"schema_intelligence"}

# Max retry attempts per step
_STEP_MAX_RETRIES = 2

# Per-step timeout (env override allowed). Generous default — agents can be slow.
_STEP_TIMEOUT_SECONDS = int(os.getenv("PIPELINE_STEP_TIMEOUT_SECONDS", "600"))

# Client DB preflight timeout (must be short to fail fast)
_PREFLIGHT_TIMEOUT_SECONDS = int(os.getenv("PIPELINE_PREFLIGHT_TIMEOUT_SECONDS", "10"))


def _to_async_url(dsn: str) -> str:
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    if dsn.startswith("postgres://"):
        return dsn.replace("postgres://", "postgresql+asyncpg://", 1)
    if dsn.startswith("mysql://"):
        return dsn.replace("mysql://", "mysql+aiomysql://", 1)
    return dsn


class PipelineOrchestrator:
    """Executes the full autonomous agent pipeline for one organisation.

    Each execution creates fresh agent instances so metrics and internal
    state don't leak between runs.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def execute(
        self,
        org_id: UUID,
        *,
        trigger_source: str = "manual",
        run_id: UUID | None = None,
    ) -> PipelineState:
        """Run the complete pipeline for one organisation."""

        run_repo = PipelineRunRepository(self._session)
        run = await self._get_or_create_run(run_repo, org_id, trigger_source, run_id)
        await run_repo.mark_running(run)
        await self._safe_commit("mark run running")

        try:
            state = await self._build_initial_state(org_id)
        except Exception as e:
            logger.error("[Pipeline] Failed to build initial state for %s: %s", org_id, e)
            await self._finalize_run(
                run_repo, run,
                status="failed",
                error=f"State init failed: {e}",
                current_step="initializing",
                started_at=time.monotonic(),
                step_metrics=[],
                state=None,
            )
            return self._empty_state(org_id, error=str(e))

        pipeline_start = time.monotonic()
        step_metrics: list[dict] = []

        # Preflight: confirm the client DB is reachable before any agent runs.
        preflight_error = await self._client_db_preflight(org_id)
        if preflight_error:
            logger.error(
                "[Pipeline] Client DB preflight failed for org %s: %s",
                org_id, preflight_error,
            )
            state["error"] = preflight_error
            state["current_step"] = "preflight_failed"
            await self._finalize_run(
                run_repo, run,
                status="failed",
                error=f"Client DB unreachable: {preflight_error}",
                current_step="preflight_failed",
                started_at=pipeline_start,
                step_metrics=step_metrics,
                state=state,
            )
            return state

        pipeline = [
            ("schema_intelligence", SchemaIntelligenceAgent()),
            ("profiling", ProfilingAgent()),
            ("risk_scoring", RiskScoringAgent()),
            ("recommendation", RecommendationAgent()),
        ]

        logger.info(
            "═══ Pipeline %s started for org '%s' (%s) ═══",
            run.id, state.get("org_name", "unknown"), org_id,
        )

        for step_name, agent in pipeline:
            state["current_step"] = step_name
            step_start = time.monotonic()
            step_error: str | None = None

            for attempt in range(1, _STEP_MAX_RETRIES + 1):
                try:
                    agent.reset_metrics()
                    state = await asyncio.wait_for(
                        agent.run(state, self._session),
                        timeout=_STEP_TIMEOUT_SECONDS,
                    )
                    step_error = state.get("error")

                    if step_error and attempt < _STEP_MAX_RETRIES:
                        logger.warning(
                            "[Pipeline] Step '%s' attempt %d failed: %s — retrying",
                            step_name, attempt, step_error,
                        )
                        state["error"] = None
                        continue
                    break

                except asyncio.TimeoutError:
                    step_error = (
                        f"Step '{step_name}' exceeded {_STEP_TIMEOUT_SECONDS}s timeout"
                    )
                    logger.error("[Pipeline] %s", step_error)
                    if attempt < _STEP_MAX_RETRIES:
                        continue
                    state["error"] = step_error
                    break

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
            agent_metrics = agent.get_metrics_summary()
            step_metrics.append({
                "step": step_name,
                "duration_ms": step_elapsed,
                "success": step_error is None,
                "error": step_error,
                **agent_metrics,
            })

            # Persist progress between steps so an in-flight run is observable.
            await self._update_run_progress(
                run_repo, run,
                current_step=step_name,
                step_metrics=step_metrics,
            )

            if step_error:
                if step_name in _NON_FATAL_STEPS:
                    logger.warning(
                        "[Pipeline] Non-fatal step '%s' failed (%dms) — continuing",
                        step_name, step_elapsed,
                    )
                    state["error"] = None
                else:
                    logger.error(
                        "[Pipeline] Fatal step '%s' failed (%dms) — aborting",
                        step_name, step_elapsed,
                    )
                    break
            else:
                logger.info(
                    "[Pipeline] Step '%s' completed in %dms "
                    "(llm_calls=%d, tool_calls=%d, tokens=%d)",
                    step_name, step_elapsed,
                    agent_metrics.get("llm_calls", 0),
                    agent_metrics.get("tool_calls", 0),
                    agent_metrics.get("total_tokens", 0),
                )

        state["completed_at"] = datetime.now(timezone.utc).isoformat()
        state["current_step"] = (
            "completed" if not state.get("error") else state.get("current_step", "failed")
        )

        # Commit any pending DB changes (recommendations, etc.) from agent runs.
        commit_error: str | None = None
        try:
            await self._session.commit()
        except Exception as e:
            logger.error("[Pipeline] Final commit failed: %s", e)
            commit_error = f"Commit failed: {e}"
            try:
                await self._session.rollback()
            except Exception:
                pass

        if commit_error and not state.get("error"):
            state["error"] = commit_error

        # Aggregate metrics, attach to state, persist to PipelineRun row.
        pipeline_metrics = _aggregate_metrics(step_metrics, pipeline_start)
        state["pipeline_metrics"] = pipeline_metrics

        await self._finalize_run(
            run_repo, run,
            status="failed" if state.get("error") else "succeeded",
            error=state.get("error"),
            current_step=state.get("current_step"),
            started_at=pipeline_start,
            step_metrics=step_metrics,
            state=state,
        )

        risk_summary = state.get("risk_summary", {})
        rec_stats = state.get("recommendation_stats", {})
        logger.info(
            "═══ Pipeline %s complete for org '%s' in %dms ═══ "
            "scored=%d critical=%d high=%d recs=%d llm=%d tools=%d tokens=%d fallbacks=%d",
            run.id, state.get("org_name", "unknown"),
            pipeline_metrics["total_duration_ms"],
            risk_summary.get("total_scored", 0),
            risk_summary.get("critical_count", 0),
            risk_summary.get("high_count", 0),
            rec_stats.get("total_generated", 0),
            pipeline_metrics["total_llm_calls"],
            pipeline_metrics["total_tool_calls"],
            pipeline_metrics["total_tokens"],
            pipeline_metrics["provider_fallbacks"],
        )

        state["pipeline_run_id"] = str(run.id)
        return state

    # ─── Run lifecycle helpers ──────────────────────────────────────────

    async def _get_or_create_run(
        self,
        repo: PipelineRunRepository,
        org_id: UUID,
        trigger_source: str,
        run_id: UUID | None,
    ) -> PipelineRun:
        if run_id is not None:
            existing = await repo.get_by_id(run_id)
            if existing is None:
                raise ValueError(f"PipelineRun {run_id} not found")
            return existing
        return await repo.create_queued(org_id, trigger_source=trigger_source)

    async def _safe_commit(self, label: str) -> None:
        try:
            await self._session.commit()
        except Exception as e:
            logger.warning("[Pipeline] Commit during %s failed: %s", label, e)
            try:
                await self._session.rollback()
            except Exception:
                pass

    async def _update_run_progress(
        self,
        repo: PipelineRunRepository,
        run: PipelineRun,
        *,
        current_step: str,
        step_metrics: list[dict],
    ) -> None:
        try:
            await repo.update(run, current_step=current_step, step_metrics=step_metrics)
            await self._session.commit()
        except SQLAlchemyError as e:
            logger.warning("[Pipeline] Failed to update run progress: %s", e)
            try:
                await self._session.rollback()
            except Exception:
                pass

    async def _finalize_run(
        self,
        repo: PipelineRunRepository,
        run: PipelineRun,
        *,
        status: str,
        error: str | None,
        current_step: str | None,
        started_at: float,
        step_metrics: list[dict],
        state: PipelineState | None,
    ) -> None:
        risk_summary = (state or {}).get("risk_summary") or {}
        rec_stats = (state or {}).get("recommendation_stats") or {}
        metrics = _aggregate_metrics(step_metrics, started_at)
        caps = (state or {}).get("generation_caps")

        try:
            await repo.finalize(
                run,
                status=status,
                error=error,
                current_step=current_step,
                duration_ms=metrics["total_duration_ms"],
                entities_scored=int(risk_summary.get("total_scored", 0) or 0),
                critical_count=int(risk_summary.get("critical_count", 0) or 0),
                high_count=int(risk_summary.get("high_count", 0) or 0),
                recommendations_generated=int(rec_stats.get("total_generated", 0) or 0),
                total_llm_calls=metrics["total_llm_calls"],
                total_tool_calls=metrics["total_tool_calls"],
                total_tokens=metrics["total_tokens"],
                provider_fallbacks=metrics["provider_fallbacks"],
                step_metrics=step_metrics,
                generation_caps=caps,
            )
            await self._session.commit()
        except SQLAlchemyError as e:
            logger.error("[Pipeline] Failed to finalize run %s: %s", run.id, e)
            try:
                await self._session.rollback()
            except Exception:
                pass

    # ─── Preflight ──────────────────────────────────────────────────────

    async def _client_db_preflight(self, org_id: UUID) -> str | None:
        """Return None on success or a short error string on failure."""
        from sqlalchemy import select

        try:
            result = await self._session.execute(
                select(Connection).where(Connection.org_id == org_id).limit(1)
            )
            conn = result.scalar_one_or_none()
            if not conn:
                return "No connection configured for this organisation"
            dsn = decrypt_dsn(conn.encrypted_dsn)
        except Exception as e:
            return f"Could not load connection: {e}"

        url = _to_async_url(dsn)
        connect_args = (
            {"connect_timeout": _PREFLIGHT_TIMEOUT_SECONDS}
            if url.startswith("mysql+aiomysql://")
            else {"timeout": _PREFLIGHT_TIMEOUT_SECONDS}
        )
        engine = create_async_engine(url, connect_args=connect_args)
        try:
            async with engine.connect() as client_conn:
                await asyncio.wait_for(
                    client_conn.execute(text("SELECT 1")),
                    timeout=_PREFLIGHT_TIMEOUT_SECONDS,
                )
            return None
        except asyncio.TimeoutError:
            return f"timed out after {_PREFLIGHT_TIMEOUT_SECONDS}s"
        except Exception as e:
            return str(e)
        finally:
            await engine.dispose()

    # ─── Initial state ──────────────────────────────────────────────────

    async def _build_initial_state(self, org_id: UUID) -> PipelineState:
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
            "current_step": "initializing",
            "error": None,
            "reasoning_log": [],
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
        }
        return state

    @staticmethod
    def _empty_state(org_id: UUID, *, error: str) -> PipelineState:
        return {
            "org_id": str(org_id),
            "error": error,
            "current_step": "failed",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "reasoning_log": [],
            "pipeline_metrics": {},
        }


def _aggregate_metrics(step_metrics: list[dict], pipeline_start: float) -> dict[str, Any]:
    total_ms = int((time.monotonic() - pipeline_start) * 1000)
    total_tokens = sum(s.get("total_tokens", 0) for s in step_metrics)
    total_llm = sum(s.get("llm_calls", 0) for s in step_metrics)
    total_tools = sum(s.get("tool_calls", 0) for s in step_metrics)
    total_fallbacks = sum(s.get("provider_fallbacks", 0) for s in step_metrics)
    all_providers: set[str] = set()
    for s in step_metrics:
        all_providers.update(s.get("providers_used", []) or [])
    return {
        "total_duration_ms": total_ms,
        "total_llm_calls": total_llm,
        "total_tool_calls": total_tools,
        "total_tokens": total_tokens,
        "provider_fallbacks": total_fallbacks,
        "providers_used": sorted(all_providers),
        "steps": step_metrics,
    }
