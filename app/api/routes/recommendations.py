import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.role_deps import require_role
from app.api.errors import bad_request, not_found
from app.infrastructure.database.models.org_notification import OrgNotification
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
        raise bad_request("BAD_REQUEST", f"Invalid {field_name}") from exc


def _rec_out(rec) -> dict:
    return {
        "id": str(rec.id),
        "entity_id": rec.entity_id,
        "entity_label": rec.entity_label,
        "type": rec.type,
        "title": rec.title,
        "urgency": rec.urgency,
        "confidence_score": float(rec.confidence_score) if rec.confidence_score is not None else None,
        "reasoning": rec.reasoning,
        "suggested_action": rec.suggested_action,
        "expected_impact": rec.expected_impact,
        "status": rec.status,
        "expires_at": rec.expires_at.isoformat() if rec.expires_at else None,
        "actioned_by": str(rec.actioned_by) if rec.actioned_by else None,
        "actioned_at": rec.actioned_at.isoformat() if rec.actioned_at else None,
        "created_at": rec.created_at.isoformat(),
    }


@router.get("")
async def list_recommendations(
    status_filter: str | None = Query(None, alias="status"),
    urgency: str | None = None,
    entity_id: str | None = None,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    repo = RecommendationRepository(db)
    offset = (page - 1) * limit
    recs = await repo.list_by_org(
        current_user.org_id,
        urgency=urgency,
        status=status_filter,
        entity_id=entity_id,
        limit=limit,
        offset=offset,
    )
    total = await repo.count_by_org(
        current_user.org_id,
        urgency=urgency,
        status=status_filter,
        entity_id=entity_id,
    )
    return {"recommendations": [_rec_out(r) for r in recs], "total": total, "page": page, "limit": limit}


@router.get("/{recommendation_id}")
async def get_recommendation(
    recommendation_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rec = await RecommendationRepository(db).get_by_id(_parse_uuid(recommendation_id, "recommendation_id"))
    if not rec or rec.org_id != current_user.org_id:
        raise not_found()
    return _rec_out(rec)


class ActionBody(BaseModel):
    outcome_notes: str | None = None


@router.post("/{recommendation_id}/action")
async def action_recommendation(
    recommendation_id: str,
    body: ActionBody | None = None,
    current_user: User = Depends(require_role("admin", "manager", "analyst")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rec = await RecommendationRepository(db).get_by_id(_parse_uuid(recommendation_id, "recommendation_id"))
    if not rec or rec.org_id != current_user.org_id:
        raise not_found()
    rec.status = "actioned"
    rec.actioned_by = current_user.id
    rec.actioned_at = datetime.now(timezone.utc)
    if body and body.outcome_notes:
        rec.outcome_notes = body.outcome_notes
    await db.commit()
    await db.refresh(rec)
    return _rec_out(rec)


class DismissBody(BaseModel):
    reason: str | None = None


@router.post("/{recommendation_id}/dismiss")
async def dismiss_recommendation(
    recommendation_id: str,
    body: DismissBody | None = None,
    current_user: User = Depends(require_role("admin", "manager", "analyst")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rec = await RecommendationRepository(db).get_by_id(_parse_uuid(recommendation_id, "recommendation_id"))
    if not rec or rec.org_id != current_user.org_id:
        raise not_found()
    if rec.status in ("actioned", "dismissed"):
        raise bad_request("BAD_REQUEST", "Already actioned or dismissed")
    rec.status = "dismissed"
    await db.commit()
    await db.refresh(rec)
    return _rec_out(rec)


@router.post("/{recommendation_id}/escalate")
async def escalate_recommendation(
    recommendation_id: str,
    current_user: User = Depends(require_role("admin", "manager", "analyst")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rec = await RecommendationRepository(db).get_by_id(_parse_uuid(recommendation_id, "recommendation_id"))
    if not rec or rec.org_id != current_user.org_id:
        raise not_found()
    if rec.status in ("actioned", "dismissed", "escalated"):
        raise bad_request("BAD_REQUEST", "Recommendation cannot be escalated in its current state")
    rec.status = "escalated"
    await db.flush()
    mgrs = await db.execute(
        select(User).where(
            User.org_id == current_user.org_id,
            User.is_active.is_(True),
            User.role.in_(("admin", "manager")),
        )
    )
    for u in mgrs.scalars().all():
        db.add(
            OrgNotification(
                org_id=current_user.org_id,
                user_id=u.id,
                title="Recommendation escalated",
                body=rec.title,
                type="warning",
                source="recommendation",
                source_id=rec.id,
            )
        )
    await db.commit()
    await db.refresh(rec)
    return _rec_out(rec)
