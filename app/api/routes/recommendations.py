import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.schemas.recommendation import RecommendationResponse, UpdateRecommendationRequest
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.recommendation_repository import (
    RecommendationRepository,
)
from app.infrastructure.database.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/recommendations", tags=["recommendations"])


def _parse_uuid(value: str, field_name: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid {field_name}") from exc


def _rec_to_response(rec) -> RecommendationResponse:
    return RecommendationResponse(
        id=rec.id,
        org_id=rec.org_id,
        entity_id=rec.entity_id,
        entity_label=rec.entity_label,
        type=rec.type,
        urgency=rec.urgency,
        title=rec.title,
        reasoning=rec.reasoning,
        suggested_action=rec.suggested_action,
        status=rec.status,
        actioned_by=rec.actioned_by,
        actioned_at=rec.actioned_at,
        created_at=rec.created_at,
    )


@router.get("", response_model=list[RecommendationResponse])
async def list_recommendations(
    urgency: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[RecommendationResponse]:
    recs = await RecommendationRepository(db).list_by_org(
        current_user.org_id, urgency=urgency, status=status_filter
    )
    return [_rec_to_response(r) for r in recs]


@router.patch("/{recommendation_id}", response_model=RecommendationResponse)
async def update_recommendation(
    recommendation_id: str,
    body: UpdateRecommendationRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RecommendationResponse:
    rec = await RecommendationRepository(db).get_by_id(
        _parse_uuid(recommendation_id, "recommendation_id")
    )
    if not rec or rec.org_id != current_user.org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Recommendation not found"
        )

    rec.status = body.status
    if body.status == "actioned":
        rec.actioned_by = current_user.id
        rec.actioned_at = datetime.now(timezone.utc)

    await db.flush()
    await db.commit()
    return _rec_to_response(rec)
