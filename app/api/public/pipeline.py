"""Public pipeline API — same trigger semantics as internal, scoped by API key org."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.api_key_auth import require_api_key
from app.api.errors import bad_request
from app.api.public.envelope import envelope
from app.infrastructure.database.repositories.pipeline_run_repository import (
    PipelineRunRepository,
)
from app.infrastructure.database.session import get_db
from app.services.pipeline_trigger import claim_and_trigger_pipeline, serialize_pipeline_run

router = APIRouter(prefix="/pipeline", tags=["Pipeline"])


class TriggerRequest(BaseModel):
    mapping_id: str | None = None


@router.post(
    "/trigger",
    status_code=202,
    summary="Trigger pipeline run",
    description="Queues a new pipeline run for your org. Requires a write-scoped API key.",
)
async def trigger_pipeline(
    body: TriggerRequest = TriggerRequest(),
    ctx=Depends(require_api_key("write")),
    db: AsyncSession = Depends(get_db),
):
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
    summary="List pipeline runs",
    description="Returns recent pipeline runs for your org.",
)
async def list_runs(
    limit: int = Query(25, ge=1, le=100),
    ctx=Depends(require_api_key("read")),
    db: AsyncSession = Depends(get_db),
):
    org_id = UUID(ctx.org_id)
    runs = await PipelineRunRepository(db).list_by_org(org_id, limit=limit)
    data = {"runs": [serialize_pipeline_run(r) for r in runs]}
    return envelope(data, ctx.org_id)
