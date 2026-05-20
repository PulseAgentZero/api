from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.api_key_auth import require_api_key
from app.api.errors import validation_error
from app.api.public.envelope import envelope
from app.api.public.schemas import (
    EntityDetailResponse,
    EntityListResponse,
    EntityRiskHistoryResponse,
    PublicErrorResponse,
    RiskPeriod,
    RiskTier,
)
from app.infrastructure.database.session import get_db
from app.services.entity_reads import (
    fetch_entity_detail,
    fetch_entity_list,
    fetch_entity_risk_history,
)

router = APIRouter(prefix="/entities", tags=["Entities"])

_READ_ERRORS = {
    401: {"model": PublicErrorResponse, "description": "Invalid or expired API key"},
    404: {"model": PublicErrorResponse, "description": "Entity not found"},
    429: {"model": PublicErrorResponse, "description": "Rate limit exceeded"},
}


@router.get(
    "",
    response_model=EntityListResponse,
    summary="List entities",
    response_description="Paginated list of profiled entities with current risk scores.",
    responses=_READ_ERRORS,
)
async def list_entities(
    page: Annotated[int, Query(ge=1, description="Page number (1-based).", examples=[1])] = 1,
    limit: Annotated[
        int,
        Query(ge=1, le=100, description="Results per page (max 100).", examples=[50]),
    ] = 50,
    risk_tier: Annotated[
        RiskTier | None,
        Query(description="Filter by risk tier."),
    ] = None,
    segment: Annotated[
        str | None,
        Query(description="Filter by segment label (exact match)."),
    ] = None,
    search: Annotated[
        str | None,
        Query(
            description="Case-insensitive search across `entity_id` and `entity_name`.",
            examples=["ACME-1042"],
        ),
    ] = None,
    ctx=Depends(require_api_key("read")),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns all **latest** entity profiles for your organization, ordered by risk score (highest first).

    Each row includes the current risk tier, narrative, and count of open recommendations.
    Use pagination when you have more than `limit` entities.
    """
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
    response_model=EntityDetailResponse,
    summary="Get entity",
    response_description="Full entity profile, risk details, and recent recommendations.",
    responses=_READ_ERRORS,
)
async def get_entity(
    entity_id: Annotated[
        str,
        Path(description="Entity identifier from your connected database (schema mapping)."),
    ],
    ctx=Depends(require_api_key("read")),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns the full behavioral profile for a single entity, including:

    - Current **risk score** and **tier**
    - **`profile_data`** — structured fields from the profiling agent
    - Up to **20** recent recommendations (any status)

    The `entity_id` must match the identifier configured in your schema mapping.
    """
    data = await fetch_entity_detail(db, UUID(ctx.org_id), entity_id)
    return envelope(data, ctx.org_id)


@router.get(
    "/{entity_id}/risk-history",
    response_model=EntityRiskHistoryResponse,
    summary="Get entity risk history",
    response_description="Time-series risk scores for charting and trend analysis.",
    responses={
        **_READ_ERRORS,
        422: {"model": PublicErrorResponse, "description": "Invalid `period` value"},
    },
)
async def get_entity_risk_history(
    entity_id: Annotated[str, Path(description="Entity identifier.")],
    period: Annotated[
        RiskPeriod,
        Query(description="Lookback window for history points."),
    ] = "30d",
    ctx=Depends(require_api_key("read")),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns chronological **risk score** and **tier** snapshots for the entity.

    Points are recorded on each pipeline run that scores the entity. Use this endpoint
    to power trend charts or detect risk escalation over time.
    """
    if period not in ("7d", "30d", "90d", "180d"):
        raise validation_error(
            "period must be one of 7d, 30d, 90d, 180d",
            fields={"period": "Must be one of: 7d, 30d, 90d, 180d"},
        )
    data = await fetch_entity_risk_history(
        db, UUID(ctx.org_id), entity_id, period=period
    )
    return envelope(data, ctx.org_id)
