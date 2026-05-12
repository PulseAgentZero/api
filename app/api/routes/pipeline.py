"""Pipeline management API routes — trigger runs and check status."""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.infrastructure.database.models.user import User
from app.infrastructure.database.session import get_db
from app.services.schedulers.pipeline_scheduler import trigger_pipeline_now

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pipeline", tags=["pipeline"])


@router.post("/run")
async def trigger_pipeline(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger an autonomous pipeline run for the current org.

    The pipeline runs asynchronously in the background. This endpoint
    returns immediately with a confirmation.
    """
    await trigger_pipeline_now(current_user.org_id)
    return {
        "message": "Pipeline run triggered",
        "org_id": str(current_user.org_id),
        "status": "running",
        "note": "Pipeline is running in background. Check server logs for progress.",
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

    orchestrator = PipelineOrchestrator(db)
    state = await orchestrator.execute(current_user.org_id)

    return {
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
