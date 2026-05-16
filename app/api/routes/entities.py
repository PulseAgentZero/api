"""Entity browse API — reads from entity_profiles (BACKEND_ROUTES §9)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.schemas.entity import EntityDetail, EntityListResponse, EntityRiskHistoryResponse
from app.infrastructure.database.models.user import User
from app.infrastructure.database.session import get_db
from app.services.entity_reads import (
    fetch_entity_detail,
    fetch_entity_list,
    fetch_entity_risk_history,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/entities", tags=["Entities"])


@router.get("", response_model=EntityListResponse)
async def list_entities(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    risk_tier: str | None = None,
    segment: str | None = None,
    search: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List profiled entities, ordered by risk score descending.

    **risk_tier** — filter by `"High"`, `"Medium"`, `"Low"`, or `"Healthy"`.
    **segment** — filter by segment label (org-specific, e.g. `"VIP"`, `"Churned"`).
    **search** — fuzzy match on entity name or ID.
    Each entity includes `open_recommendations` count.
    """
    return await fetch_entity_list(
        db,
        current_user.org_id,
        page=page,
        limit=limit,
        risk_tier=risk_tier,
        segment=segment,
        search=search,
    )


@router.get("/{entity_id}", response_model=EntityDetail)
async def get_entity(
    entity_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return full profile detail for a single entity.

    Includes `profile_data` (raw signal values from the client DB), `risk_narrative`
    (LLM-generated explanation), and the 20 most recent recommendations for this entity.
    """
    return await fetch_entity_detail(db, current_user.org_id, entity_id)


@router.get("/{entity_id}/risk-history", response_model=EntityRiskHistoryResponse)
async def entity_risk_history(
    entity_id: str,
    period: str = Query("30d", pattern="^(7d|30d|90d|180d)$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return the risk score time series for an entity. Useful for sparklines and trend charts.

    **period** — lookback window: `7d`, `30d`, `90d`, `180d` (default `30d`).
    Each point contains `risk_score`, `risk_tier`, and `recorded_at` (ISO-8601 timestamp).
    """
    return await fetch_entity_risk_history(
        db, current_user.org_id, entity_id, period=period
    )
