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
import dataclasses
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.agents.state import PipelineState
from app.agents.workflows.schema_intelligence_agent import SchemaIntelligenceAgent
from app.agents.workflows.profiling_agent import ProfilingAgent
from app.agents.workflows.model_training_agent import ModelTrainingAgent
from app.agents.workflows.risk_scoring_agent import RiskScoringAgent
from app.agents.workflows.recommendation_agent import RecommendationAgent
from app.infrastructure.crypto import decrypt_dsn
from app.infrastructure.connectors.payload import parse_pulse_api_payload
from app.infrastructure.database.client_queries import get_schema_mapping
from app.infrastructure.database.models.connection import Connection
from app.infrastructure.database.models.pipeline_run import PipelineRun
from app.infrastructure.database.sql_connect import (
    connect_args_for_async_url,
    is_likely_async_sqlalchemy_url,
    sync_dsn_to_async_sqlalchemy_url,
)
from app.infrastructure.database.repositories.organization_repository import (
    OrganizationRepository,
)
from app.infrastructure.database.repositories.pipeline_run_repository import (
    PipelineRunRepository,
)
from app.infrastructure.database.repositories.schema_mapping_repository import (
    SchemaMappingRepository,
)
from app.services.procedural_memory import (
    extract_and_commit_from_run,
    format_learnings_for_prompt,
    recall_learnings,
)

logger = logging.getLogger(__name__)

# Steps that can fail without aborting the entire pipeline
_NON_FATAL_STEPS = {"schema_intelligence", "model_training"}

# Max retry attempts per step
_STEP_MAX_RETRIES = 2

# Per-step timeout (env override allowed). Generous default — agents can be slow.
_STEP_TIMEOUT_SECONDS = int(os.getenv("PIPELINE_STEP_TIMEOUT_SECONDS", "600"))

# Client DB preflight timeout (must be short to fail fast)
_PREFLIGHT_TIMEOUT_SECONDS = int(os.getenv("PIPELINE_PREFLIGHT_TIMEOUT_SECONDS", "10"))
_RUN_LOG_DIR = Path(os.getenv("PIPELINE_RUN_LOG_DIR", "logs/pipeline_runs"))


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
            state = await self._build_initial_state(org_id, mapping_id=run.mapping_id)
            state["pipeline_run_id"] = str(run.id)
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
            _write_run_artifacts(
                run,
                status="failed",
                error=f"State init failed: {e}",
                org_id=org_id,
                org_name=None,
                current_step="initializing",
                duration_ms=0,
                step_metrics=[],
                state=None,
                rag_metrics=None,
            )
            return self._empty_state(org_id, error=str(e))
        if run.mapping_id is None and state.get("mapping_id"):
            run.mapping_id = UUID(str(state["mapping_id"]))
            await self._safe_commit("attach run mapping")

        pipeline_start = time.monotonic()
        step_metrics: list[dict] = []

        # Preflight: confirm the client DB is reachable before any agent runs.
        preflight_error = await self._client_db_preflight(
            org_id,
            connection_id=UUID(str(state["connection_id"])),
        )
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
            _write_run_artifacts(
                run,
                status="failed",
                error=f"Client DB unreachable: {preflight_error}",
                org_id=org_id,
                org_name=state.get("org_name"),
                current_step="preflight_failed",
                duration_ms=0,
                step_metrics=step_metrics,
                state=state,
                rag_metrics=None,
            )
            return state

        # Procedural memory recall: surface learnings from prior runs so downstream
        # agents (and operators looking at logs) can see what's worked before.
        try:
            recall_query = " ".join(
                str(state.get(k) or "")
                for k in ("org_name", "industry", "entity_label", "goal_label", "business_context")
            ).strip()
            learnings = await recall_learnings(org_id, recall_query) if recall_query else []
            if learnings:
                state["procedural_learnings"] = [
                    (m.payload or {}).get("content", "") for m in learnings
                ]
                logger.info(
                    "[Pipeline] Recalled %d procedural learnings for org %s:\n%s",
                    len(learnings), org_id,
                    format_learnings_for_prompt(learnings).rstrip(),
                )
        except Exception as exc:
            logger.debug("[Pipeline] procedural recall skipped (non-fatal): %s", exc)

        pipeline = [
            ("schema_intelligence", SchemaIntelligenceAgent()),
            ("profiling", ProfilingAgent()),
            ("model_training", ModelTrainingAgent()),
            ("risk_scoring", RiskScoringAgent()),
            ("recommendation", RecommendationAgent()),
        ]

        logger.info(
            "═══ Pipeline %s started for org '%s' (%s) ═══",
            run.id, state.get("org_name", "unknown"), org_id,
        )
        _write_run_artifacts(
            run,
            status="running",
            error=None,
            org_id=org_id,
            org_name=state.get("org_name"),
            current_step=state.get("current_step"),
            duration_ms=0,
            step_metrics=step_metrics,
            state=state,
            rag_metrics=None,
        )

        for step_name, agent in pipeline:
            state["current_step"] = step_name
            step_start = time.monotonic()
            step_error: str | None = None
            _write_run_artifacts(
                run,
                status="running",
                error=None,
                org_id=org_id,
                org_name=state.get("org_name"),
                current_step=step_name,
                duration_ms=int((time.monotonic() - pipeline_start) * 1000),
                step_metrics=step_metrics,
                state=state,
                rag_metrics=None,
            )

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
            _write_run_artifacts(
                run,
                status="running",
                error=state.get("error"),
                org_id=org_id,
                org_name=state.get("org_name"),
                current_step=step_name,
                duration_ms=int((time.monotonic() - pipeline_start) * 1000),
                step_metrics=step_metrics,
                state=state,
                rag_metrics=None,
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

        if not state.get("error"):
            try:
                mapping = await self._get_schema_mapping(org_id, run.mapping_id)
                from app.services.entity_profile_persist import (
                    persist_entity_profiles_from_pipeline,
                )

                await persist_entity_profiles_from_pipeline(
                    self._session,
                    org_id=org_id,
                    run_id=run.id,
                    mapping_id=mapping.id,
                    state=state,
                )
                run.mapping_id = mapping.id
            except Exception as e:
                logger.warning("[Pipeline] entity profile persist skipped: %s", e)

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

        # Collect RAG latency stats accumulated by risk/rec agents into state.
        rag_stats_raw: dict[str, Any] = state.get("rag_run_stats") or {}
        rag_metrics_payload: dict[str, Any] = {"latency": rag_stats_raw}

        # RAG eval regression + TTL cleanup — both non-fatal.
        try:
            from app.services.rag_eval import run_rag_eval_regression

            eval_report = await run_rag_eval_regression(str(org_id))
            rag_metrics_payload["eval"] = (
                dataclasses.asdict(eval_report) if not eval_report.skipped else {"skipped": True}
            )
        except Exception as e:
            logger.warning("[Pipeline] RAG eval regression failed: %s", e)
            rag_metrics_payload["eval"] = {"error": str(e)}

        try:
            from app.infrastructure.external_services.rag import run_ttl_cleanup

            ttl_removed = await run_ttl_cleanup(str(org_id))
            rag_metrics_payload["ttl_cleaned"] = ttl_removed
        except Exception as e:
            logger.warning("[Pipeline] Qdrant TTL cleanup failed: %s", e)

        state["rag_metrics"] = rag_metrics_payload

        await self._finalize_run(
            run_repo, run,
            status="failed" if state.get("error") else "succeeded",
            error=state.get("error"),
            current_step=state.get("current_step"),
            started_at=pipeline_start,
            step_metrics=step_metrics,
            state=state,
            rag_metrics=rag_metrics_payload,
        )

        if state.get("error"):
            try:
                from app.services.notification_service import notify_pipeline_failed

                await notify_pipeline_failed(
                    self._session,
                    org_id,
                    pipeline_run_id=run.id,
                    error_message=state.get("error"),
                )
                await self._session.commit()
            except Exception as e:
                logger.warning("[Pipeline] Failure notification skipped: %s", e)
                try:
                    await self._session.rollback()
                except Exception:
                    pass
        else:
            try:
                from app.services.alert_evaluation import evaluate_alerts_after_pipeline

                await evaluate_alerts_after_pipeline(self._session, org_id, run.id)
                await self._session.commit()
            except Exception as e:
                logger.warning("[Pipeline] Alert evaluation failed: %s", e)
                try:
                    await self._session.rollback()
                except Exception:
                    pass

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
        _write_run_artifacts(
            run,
            status="failed" if state.get("error") else "succeeded",
            error=state.get("error"),
            org_id=org_id,
            org_name=state.get("org_name"),
            current_step=state.get("current_step"),
            duration_ms=pipeline_metrics["total_duration_ms"],
            step_metrics=step_metrics,
            state=state,
            rag_metrics=rag_metrics_payload,
        )

        if trigger_source == "scheduled":
            try:
                from app.services.schedulers.pipeline_scheduler import (
                    touch_pipeline_schedule_after_run,
                )

                await touch_pipeline_schedule_after_run(org_id)
            except Exception as e:
                logger.warning(
                    "[Pipeline] Failed to update schedule timestamps for org %s: %s",
                    org_id,
                    e,
                )

        # Procedural memory commit: extract a durable learning from this run.
        # Best-effort; never blocks the response or surfaces errors to callers.
        try:
            run_summary = {
                "org_id": str(org_id),
                "org_name": state.get("org_name"),
                "industry": state.get("industry"),
                "status": "failed" if state.get("error") else "succeeded",
                "duration_ms": pipeline_metrics.get("total_duration_ms"),
                "risk_summary": risk_summary,
                "recommendation_stats": rec_stats,
                "rag_eval": rag_metrics_payload.get("eval"),
                "step_summary": [
                    {"step": s.get("step"), "ms": s.get("duration_ms"), "ok": s.get("success")}
                    for s in step_metrics
                ],
            }
            await extract_and_commit_from_run(org_id, run_summary)
        except Exception as exc:
            logger.debug("[Pipeline] procedural extract skipped (non-fatal): %s", exc)

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

    async def _get_schema_mapping(self, org_id: UUID, mapping_id: UUID | None):
        if mapping_id is None:
            return await get_schema_mapping(self._session, org_id)
        mapping = await SchemaMappingRepository(self._session).get_by_id(mapping_id)
        if mapping is None or mapping.org_id != org_id:
            raise ValueError(f"SchemaMapping {mapping_id} not found")
        return mapping

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
        rag_metrics: dict[str, Any] | None = None,
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
                rag_metrics=rag_metrics,
            )
            await self._session.commit()
        except SQLAlchemyError as e:
            logger.error("[Pipeline] Failed to finalize run %s: %s", run.id, e)
            try:
                await self._session.rollback()
            except Exception:
                pass

    # ─── Preflight ──────────────────────────────────────────────────────

    async def _client_db_preflight(
        self, org_id: UUID, *, connection_id: UUID | None = None
    ) -> str | None:
        """Return None on success or a short error string on failure."""
        from sqlalchemy import select

        try:
            stmt = select(Connection).where(
                Connection.org_id == org_id,
                Connection.deleted_at.is_(None),
            )
            if connection_id is not None:
                stmt = stmt.where(Connection.id == connection_id)
            stmt = stmt.limit(1)
            result = await self._session.execute(stmt)
            conn = result.scalar_one_or_none()
            if not conn:
                if connection_id is not None:
                    return "Mapped connection is missing or has been deleted"
                return "No connection configured for this organisation"
            from app.services.studio_file_source_service import (
                fetch_file_source_schema,
                supports_studio_file_queries,
            )

            if supports_studio_file_queries(conn):
                await fetch_file_source_schema(conn)
                return None
            if not conn.encrypted_dsn:
                return (
                    "Connection has no stored credentials. Save or re-create "
                    "the connection, then run the pipeline again."
                )
            dsn = decrypt_dsn(conn.encrypted_dsn)
            if parse_pulse_api_payload(dsn) is not None:
                return (
                    "Connection is an API or object-store connector; "
                    "the agent pipeline requires a SQL database connection"
                )
        except Exception as e:
            return f"Could not load connection: {e}"

        url = sync_dsn_to_async_sqlalchemy_url(dsn)
        if not is_likely_async_sqlalchemy_url(url):
            return "Connection URL is not supported for SQL pipeline preflight"
        engine = create_async_engine(
            url,
            connect_args=connect_args_for_async_url(url, conn.sslmode),
        )
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

    async def _build_initial_state(
        self, org_id: UUID, *, mapping_id: UUID | None = None
    ) -> PipelineState:
        org_repo = OrganizationRepository(self._session)
        org = await org_repo.get_by_id(org_id)
        if not org:
            raise ValueError(f"Organisation {org_id} not found")

        mapping = await self._get_schema_mapping(org_id, mapping_id)

        state: PipelineState = {
            "org_id": str(org_id),
            "org_name": org.name,
            "mapping_id": str(mapping.id),
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
            # Model Training Agent defaults
            "target_column": getattr(mapping, "target_column", None),
            "ml_available": False,
            "model_metrics": {},
            "feature_importances": [],
            "ml_scored_entities": [],
            # Control flow
            "current_step": "initializing",
            "error": None,
            "reasoning_log": [],
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
            # Cross-agent scratchpad. Agents can read/write freely; cleared when state is GC'd.
            "working_memory": {},
            "procedural_learnings": [],
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


def _write_run_artifacts(
    run: PipelineRun,
    *,
    status: str,
    error: str | None,
    org_id: UUID,
    org_name: str | None,
    current_step: str | None,
    duration_ms: int,
    step_metrics: list[dict],
    state: PipelineState | None,
    rag_metrics: dict[str, Any] | None,
) -> None:
    """Write compact per-run artifacts for local debugging outside Docker logs."""
    try:
        _RUN_LOG_DIR.mkdir(parents=True, exist_ok=True)
        safe_org = _slug(org_name or str(org_id))
        stem = f"{safe_org}_{run.id}"
        is_terminal = status in {"succeeded", "failed", "cancelled"}
        now_iso = datetime.now(timezone.utc).isoformat()
        artifact = {
            "run_id": str(run.id),
            "org_id": str(org_id),
            "org_name": org_name,
            "mapping_id": str(run.mapping_id) if run.mapping_id else (state or {}).get("mapping_id"),
            "trigger_source": run.trigger_source,
            "status": status,
            "current_step": current_step,
            "error": error,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "updated_at": now_iso,
            "completed_at": now_iso if is_terminal else None,
            "duration_ms": duration_ms,
            "step_metrics": step_metrics,
            "risk_summary": (state or {}).get("risk_summary") or {},
            "recommendation_stats": (state or {}).get("recommendation_stats") or {},
            "profile_stats": (state or {}).get("profile_stats") or {},
            "model_metrics": (state or {}).get("model_metrics") or {},
            "generation_caps": (state or {}).get("generation_caps") or {},
            "pipeline_metrics": (state or {}).get("pipeline_metrics") or {},
            "rag_metrics": rag_metrics or {},
            "procedural_learnings": (state or {}).get("procedural_learnings") or [],
            "counts": {
                "entity_profiles": len((state or {}).get("entity_profiles") or []),
                "scored_entities": len((state or {}).get("scored_entities") or []),
                "recommendations": len((state or {}).get("recommendations") or []),
            },
            "samples": {
                "scored_entities": ((state or {}).get("scored_entities") or [])[:20],
                "recommendations": ((state or {}).get("recommendations") or [])[:20],
                "reasoning_log": ((state or {}).get("reasoning_log") or [])[:20],
            },
        }
        json_path = _RUN_LOG_DIR / f"{stem}.json"
        txt_path = _RUN_LOG_DIR / f"{stem}.log"
        json_path.write_text(json.dumps(artifact, indent=2, default=str), encoding="utf-8")
        txt_path.write_text(_format_run_log(artifact), encoding="utf-8")
        logger.info("[Pipeline] Wrote run artifacts: %s and %s", json_path, txt_path)
    except Exception as exc:
        logger.warning("[Pipeline] Failed to write run artifacts: %s", exc)


def _format_run_log(artifact: dict[str, Any]) -> str:
    lines = [
        f"Pipeline Run: {artifact['run_id']}",
        f"Org: {artifact.get('org_name')} ({artifact.get('org_id')})",
        f"Status: {artifact.get('status')} step={artifact.get('current_step')}",
        f"Duration: {artifact.get('duration_ms')}ms",
        f"Error: {artifact.get('error') or '-'}",
        "",
        "Step Metrics:",
    ]
    for step in artifact.get("step_metrics") or []:
        lines.append(
            f"- {step.get('step')}: ok={step.get('success')} "
            f"ms={step.get('duration_ms')} llm={step.get('llm_calls', 0)} "
            f"tools={step.get('tool_calls', 0)} tokens={step.get('total_tokens', 0)} "
            f"error={step.get('error') or '-'}"
        )
    lines.extend([
        "",
        f"Risk Summary: {json.dumps(artifact.get('risk_summary') or {}, default=str)}",
        f"Recommendation Stats: {json.dumps(artifact.get('recommendation_stats') or {}, default=str)}",
        f"Profile Stats: {json.dumps(artifact.get('profile_stats') or {}, default=str)}",
        f"RAG Metrics: {json.dumps(artifact.get('rag_metrics') or {}, default=str)}",
        "",
        "Full samples and reasoning snippets are in the matching JSON artifact.",
        "",
    ])
    return "\n".join(lines)


def _slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")[:48] or "org"
