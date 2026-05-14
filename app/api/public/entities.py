from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.api_key_auth import require_api_key
from app.api.errors import validation_error
from app.api.public.envelope import envelope
from app.infrastructure.database.session import get_db
from app.services.entity_reads import (
    fetch_entity_detail,
    fetch_entity_list,
    fetch_entity_risk_history,
)

router = APIRouter(prefix="/entities", tags=["Entities"])


@router.get(
    "",
    summary="List entities",
    description="Returns all profiled entities for your org with their current risk scores.",
)
async def list_entities(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    risk_tier: str | None = Query(None, description="High | Medium | Low | Healthy"),
    segment: str | None = Query(None),
    search: str | None = Query(None),
    ctx=Depends(require_api_key("read")),
    db: AsyncSession = Depends(get_db),
):
    data = await fetch_entity_list(
        db,
        UUID(ctx.org_id),
        page=page,
        limit=limit,
        risk_tier=risk_tier,
        segment=segment,
        search=search,
    )
    return envelope(data, ctx.org_id)


@router.get(
    "/{entity_id}",
    summary="Get entity",
    description="Returns full profile, risk score, and open recommendations for a single entity.",
)
async def get_entity(
    entity_id: str,
    ctx=Depends(require_api_key("read")),
    db: AsyncSession = Depends(get_db),
):
    data = await fetch_entity_detail(db, UUID(ctx.org_id), entity_id)
    return envelope(data, ctx.org_id)


@router.get(
    "/{entity_id}/risk-history",
    summary="Get entity risk history",
    description="Returns time-series risk scores for an entity over the requested period.",
)
async def get_entity_risk_history(
    entity_id: str,
    period: str = Query("30d", description="7d | 30d | 90d | 180d"),
    ctx=Depends(require_api_key("read")),
    db: AsyncSession = Depends(get_db),
):
    if period not in ("7d", "30d", "90d", "180d"):
        raise validation_error(
            "period must be one of 7d, 30d, 90d, 180d",
            fields={"period": "Must be one of: 7d, 30d, 90d, 180d"},
        )
    data = await fetch_entity_risk_history(
        db, UUID(ctx.org_id), entity_id, period=period
    )
    return envelope(data, ctx.org_id)
