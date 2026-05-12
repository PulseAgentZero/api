import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.schemas.entity import (
    EntityDetail,
    EntityListResponse,
    EntitySummary,
    EntityTrendPoint,
    EntityTrendResponse,
)
from app.infrastructure.database.client_queries import (
    ClientDBError,
    compute_risk,
    fetch_entities,
    fetch_entity_by_id,
    fetch_entity_trend,
    get_schema_mapping,
)
from app.infrastructure.database.models.user import User
from app.infrastructure.database.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/entities", tags=["entities"])


@router.get("", response_model=EntityListResponse)
async def list_entities(
    search: str | None = Query(None, description="Search by entity label"),
    risk_tier: str | None = Query(None, description="Filter by risk tier"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EntityListResponse:
    try:
        mapping = await get_schema_mapping(db, current_user.org_id)
        entities = await fetch_entities(db, current_user.org_id, mapping)
        entities = compute_risk(entities, mapping.signal_columns, mapping.risk_config)
    except ClientDBError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    id_col = mapping.entity_id_col
    name_col = mapping.entity_name_col

    summaries = [
        EntitySummary(
            entity_id=e[id_col],
            entity_label=e.get(name_col) if name_col else None,
            risk_score=e["risk_score"],
            risk_tier=e["risk_tier"],
            signals=e.get("signals", {}),
        )
        for e in entities
    ]

    if search and name_col:
        q = search.lower()
        summaries = [s for s in summaries if s.entity_label and q in s.entity_label.lower()]
    if risk_tier:
        summaries = [s for s in summaries if s.risk_tier == risk_tier]

    total = len(summaries)
    start = (page - 1) * page_size
    paged = summaries[start : start + page_size]

    return EntityListResponse(
        entities=paged,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{entity_id}", response_model=EntityDetail)
async def get_entity(
    entity_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EntityDetail:
    try:
        mapping = await get_schema_mapping(db, current_user.org_id)
        entity = await fetch_entity_by_id(db, current_user.org_id, entity_id, mapping)
    except ClientDBError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    if entity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entity not found")

    entities = compute_risk([entity], mapping.signal_columns, mapping.risk_config)
    e = entities[0]

    id_col = mapping.entity_id_col
    name_col = mapping.entity_name_col

    return EntityDetail(
        entity_id=e[id_col],
        entity_label=e.get(name_col) if name_col else None,
        risk_score=e["risk_score"],
        risk_tier=e["risk_tier"],
        signals=e.get("signals", {}),
        fields={k: v for k, v in e.items() if k not in ("risk_score", "risk_tier", "signals")},
    )


@router.get("/{entity_id}/trend", response_model=EntityTrendResponse)
async def get_entity_trend(
    entity_id: str,
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EntityTrendResponse:
    try:
        mapping = await get_schema_mapping(db, current_user.org_id)
        points = await fetch_entity_trend(db, current_user.org_id, entity_id, mapping, limit)
    except ClientDBError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    return EntityTrendResponse(
        entity_id=entity_id,
        points=[EntityTrendPoint(**point) for point in points],
    )
