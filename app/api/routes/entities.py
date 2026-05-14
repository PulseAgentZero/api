"""Entity browse API — reads from entity_profiles (BACKEND_ROUTES §9)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.infrastructure.database.models.user import User
from app.infrastructure.database.session import get_db
from app.services.entity_reads import (
    fetch_entity_detail,
    fetch_entity_list,
    fetch_entity_risk_history,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/entities", tags=["Entities"])


@router.get("")
async def list_entities(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    risk_tier: str | None = None,
    segment: str | None = None,
    search: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await fetch_entity_list(
        db,
        current_user.org_id,
        page=page,
        limit=limit,
        risk_tier=risk_tier,
        segment=segment,
        search=search,
    )


@router.get("/{entity_id}")
async def get_entity(
    entity_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await fetch_entity_detail(db, current_user.org_id, entity_id)


@router.get("/{entity_id}/risk-history")
async def entity_risk_history(
    entity_id: str,
    period: str = Query("30d", pattern="^(7d|30d|90d|180d)$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await fetch_entity_risk_history(
        db, current_user.org_id, entity_id, period=period
    )
