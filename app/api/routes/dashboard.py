import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.schemas.dashboard import OverviewResponse, RiskBreakdown, TopEntity
from app.infrastructure.database.client_queries import (
    ClientDBError,
    compute_risk,
    fetch_entities,
    get_schema_mapping,
)
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.recommendation_repository import (
    RecommendationRepository,
)
from app.infrastructure.database.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/overview", response_model=OverviewResponse)
async def get_overview(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OverviewResponse:
    try:
        mapping = await get_schema_mapping(db, current_user.org_id)
        entities = await fetch_entities(db, current_user.org_id, mapping)
        entities = compute_risk(entities, mapping.signal_columns, mapping.risk_config)
    except ClientDBError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    breakdown = RiskBreakdown(critical=0, high=0, medium=0, low=0)
    for e in entities:
        tier = e["risk_tier"]
        setattr(breakdown, tier, getattr(breakdown, tier) + 1)

    sorted_entities = sorted(entities, key=lambda e: e["risk_score"], reverse=True)
    top5 = [
        TopEntity(
            entity_id=e[mapping.entity_id_col],
            entity_label=e.get(mapping.entity_name_col) if mapping.entity_name_col else None,
            risk_score=e["risk_score"],
            risk_tier=e["risk_tier"],
        )
        for e in sorted_entities[:5]
    ]

    active_recs = await RecommendationRepository(db).list_by_org(
        current_user.org_id, status="active"
    )

    return OverviewResponse(
        total_entities=len(entities),
        risk_breakdown=breakdown,
        top_at_risk=top5,
        active_recommendations=len(active_recs),
    )
