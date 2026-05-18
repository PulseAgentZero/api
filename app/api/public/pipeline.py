"""Public pipeline API — same trigger semantics as internal, scoped by API key org."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.api_key_auth import require_api_key
from app.api.errors import bad_request
from app.api.public.envelope import envelope
from app.api.public.schemas import (
    PipelineRunListResponse,
    PipelineTriggerRequest,
    PipelineTriggerResponse,
    PublicErrorResponse,
)
from app.infrastructure.database.repositories.pipeline_run_repository import (
    PipelineRunRepository,
)
from app.infrastructure.database.session import get_db
from app.services.pipeline_trigger import claim_and_trigger_pipeline, serialize_pipeline_run

router = APIRouter(prefix="/pipeline", tags=["Pipeline"])

_READ_ERRORS = {
    401: {"model": PublicErrorResponse, "description": "Invalid or expired API key"},
    429: {"model": PublicErrorResponse, "description": "Rate limit exceeded"},
}

_WRITE_ERRORS = {
    **_READ_ERRORS,
    403: {"model": PublicErrorResponse, "description": "Write scope required"},
    409: {"model": PublicErrorResponse, "description": "Pipeline already queued or running"},
}


@router.post(
    "/trigger",
    status_code=202,
    response_model=PipelineTriggerResponse,
    summary="Trigger pipeline run",
    response_description="Pipeline run queued successfully.",
    responses=_WRITE_ERRORS,
)
async def trigger_pipeline(
    body: PipelineTriggerRequest = PipelineTriggerRequest(),
    ctx=Depends(require_api_key("write")),
    db: AsyncSession = Depends(get_db),
):
    """
    Queues a new **autonomous pipeline run** for your organization (schema profiling → risk scoring → recommendations).

    - Returns **202 Accepted** with a `run_id` — poll **`GET /v1/pipeline/runs`** for progress.
    - Only **one** run may be `queued` or `running` per org at a time (**409** otherwise).
    - Requires a **write-scoped** API key.

    Optionally pass **`mapping_id`** to target a specific schema mapping instead of the default.
    """
    org_id = UUID(ctx.org_id)
    mid: UUID | None = None
    if body.mapping_id:
        try:
            mid = UUID(body.mapping_id)
        except ValueError as exc:
            raise bad_request("BAD_REQUEST", "Invalid mapping_id") from exc
    result = await claim_and_trigger_pipeline(
        db,
        org_id,
        mapping_id=mid,
        triggered_by=None,
        trigger_source="api_key",
    )
    return envelope(result, ctx.org_id)


@router.get(
    "/runs",
    response_model=PipelineRunListResponse,
    summary="List pipeline runs",
    response_description="Recent pipeline runs, newest first.",
    responses=_READ_ERRORS,
)
async def list_runs(
    limit: Annotated[
        int,
        Query(ge=1, le=100, description="Maximum runs to return (max 100).", examples=[25]),
    ] = 25,
    ctx=Depends(require_api_key("read")),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns recent pipeline runs including status, timing, and agent metrics.

    Use this after **`POST /trigger`** to wait until `status` is `completed` or `failed`.
    """
    org_id = UUID(ctx.org_id)
    runs = await PipelineRunRepository(db).list_by_org(org_id, limit=limit)
    data = {"runs": [serialize_pipeline_run(r) for r in runs]}
    return envelope(data, ctx.org_id)
