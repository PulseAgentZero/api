"""Pipeline management API routes — trigger runs and inspect run history."""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.pipeline_run_repository import (
    PipelineRunRepository,
)
from app.infrastructure.database.session import get_db
from app.services.schedulers.pipeline_scheduler import trigger_pipeline_now

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pipeline", tags=["pipeline"])


def _serialize_run(run) -> dict:
    return {
        "id": str(run.id),
        "org_id": str(run.org_id),
        "status": run.status,
        "trigger_source": run.trigger_source,
        "current_step": run.current_step,
        "error": run.error,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "duration_ms": run.duration_ms,
        "entities_scored": run.entities_scored,
        "critical_count": run.critical_count,
        "high_count": run.high_count,
        "recommendations_generated": run.recommendations_generated,
        "total_llm_calls": run.total_llm_calls,
        "total_tool_calls": run.total_tool_calls,
        "total_tokens": run.total_tokens,
        "provider_fallbacks": run.provider_fallbacks,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


@router.post("/run", status_code=status.HTTP_202_ACCEPTED)
async def trigger_pipeline(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Trigger an autonomous pipeline run for the current org.

    Returns 202 with the new run_id, or 409 if another run is already active.
    The pipeline executes asynchronously in the background.
    """
    repo = PipelineRunRepository(db)
    active = await repo.get_active_for_org(current_user.org_id)
    if active is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Pipeline run already in progress",
                "run_id": str(active.id),
                "status": active.status,
                "current_step": active.current_step,
            },
        )

    run_id = await trigger_pipeline_now(
        current_user.org_id, trigger_source="manual"
    )
    if run_id is None:
        # Race: another worker claimed the slot between our check and the trigger.
        active = await repo.get_active_for_org(current_user.org_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Pipeline run already in progress",
                "run_id": str(active.id) if active else None,
                "status": active.status if active else "unknown",
            },
        )

    return {
        "message": "Pipeline run triggered",
        "run_id": str(run_id),
        "org_id": str(current_user.org_id),
        "status": "queued",
    }


@router.post("/run/sync")
async def trigger_pipeline_sync(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Run the pipeline synchronously and return results.

    Use this for testing/demo — blocks until the pipeline completes.
    Not recommended for production use on large datasets.
    """
    from app.agents.orchestrators.pipeline import PipelineOrchestrator

    repo = PipelineRunRepository(db)
    active = await repo.get_active_for_org(current_user.org_id)
    if active is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Pipeline run already in progress",
                "run_id": str(active.id),
                "status": active.status,
            },
        )

    orchestrator = PipelineOrchestrator(db)
    state = await orchestrator.execute(
        current_user.org_id, trigger_source="manual_sync"
    )

    return {
        "run_id": state.get("pipeline_run_id"),
        "status": state.get("current_step", "unknown"),
        "org_id": str(current_user.org_id),
        "org_name": state.get("org_name"),
        "error": state.get("error"),
        "risk_summary": state.get("risk_summary", {}),
        "recommendation_stats": state.get("recommendation_stats", {}),
        "pipeline_metrics": state.get("pipeline_metrics", {}),
        "started_at": state.get("started_at"),
        "completed_at": state.get("completed_at"),
    }


@router.get("/runs")
async def list_pipeline_runs(
    limit: int = 25,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the most recent pipeline runs for the current org."""
    limit = max(1, min(limit, 100))
    runs = await PipelineRunRepository(db).list_by_org(current_user.org_id, limit=limit)
    return {"runs": [_serialize_run(r) for r in runs]}


@router.get("/runs/{run_id}")
async def get_pipeline_run(
    run_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return detail for one pipeline run, including step metrics."""
    try:
        rid = UUID(run_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid run_id"
        )

    run = await PipelineRunRepository(db).get_by_id(rid)
    if run is None or run.org_id != current_user.org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline run not found"
        )

    payload = _serialize_run(run)
    payload["step_metrics"] = run.step_metrics or []
    payload["generation_caps"] = run.generation_caps or {}
    return payload
