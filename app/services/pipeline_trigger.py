"""Shared pipeline trigger logic (internal JWT routes + public API key routes)."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.repositories.pipeline_run_repository import PipelineRunRepository
from app.services.schedulers.pipeline_scheduler import trigger_pipeline_now

def serialize_pipeline_run(run) -> dict:
    def _iso(dt) -> str | None:
        return dt.isoformat() if dt is not None else None

    return {
        "id": str(run.id),
        "org_id": str(run.org_id),
        "status": run.status,
        "trigger_source": run.trigger_source,
        "triggered_by": str(run.triggered_by) if run.triggered_by else None,
        "mapping_id": str(run.mapping_id) if run.mapping_id else None,
        "current_step": run.current_step,
        "error": run.error,
        "started_at": _iso(run.started_at),
        "completed_at": _iso(run.completed_at),
        "duration_ms": run.duration_ms,
        "entities_scored": run.entities_scored,
        "critical_count": run.critical_count,
        "high_count": run.high_count,
        "recommendations_generated": run.recommendations_generated,
        "total_llm_calls": run.total_llm_calls,
        "total_tool_calls": run.total_tool_calls,
        "total_tokens": run.total_tokens,
        "provider_fallbacks": run.provider_fallbacks,
        "created_at": _iso(run.created_at),
    }


async def claim_and_trigger_pipeline(
    db: AsyncSession,
    org_id: UUID,
    *,
    mapping_id: UUID | None = None,
    triggered_by: UUID | None = None,
    trigger_source: str = "manual",
) -> dict:
    """Create a queued run if none active, enqueue/execute pipeline, optionally set mapping/trigger user.

    Raises HTTPException 409 if a run is already queued/running.
    """
    repo = PipelineRunRepository(db)
    active = await repo.get_active_for_org(org_id)
    if active is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "PIPELINE_ALREADY_RUNNING",
                "message": "Pipeline run already in progress",
                "run_id": str(active.id),
                "status": active.status,
                "current_step": active.current_step,
            },
        )

    run_id = await trigger_pipeline_now(
        org_id,
        trigger_source=trigger_source,
        mapping_id=mapping_id,
        triggered_by=triggered_by,
    )
    if run_id is None:
        active = await repo.get_active_for_org(org_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "PIPELINE_ALREADY_RUNNING",
                "message": "Pipeline run already in progress",
                "run_id": str(active.id) if active else None,
                "status": active.status if active else "unknown",
            },
        )

    return {
        "run_id": str(run_id),
        "status": "queued",
        "message": "Pipeline queued",
    }
