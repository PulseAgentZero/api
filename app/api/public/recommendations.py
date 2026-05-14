from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.api_key_auth import require_api_key
from app.api.errors import bad_request, not_found
from app.api.public.envelope import envelope
from app.infrastructure.database.repositories.recommendation_repository import RecommendationRepository
from app.infrastructure.database.session import get_db

router = APIRouter(prefix="/recommendations", tags=["Recommendations"])


class ActionRequest(BaseModel):
    outcome_notes: str | None = None


class DismissRequest(BaseModel):
    reason: str | None = None


def _rid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise bad_request("BAD_REQUEST", "Invalid recommendation id") from exc


def _rec_dict(rec) -> dict:
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
        "created_at": rec.created_at.isoformat(),
    }


@router.get(
    "",
    summary="List recommendations",
    description="Returns recommendations for your org. Filter by status, urgency, or entity.",
)
async def list_recommendations(
    status: str | None = Query(None, description="open | actioned | dismissed | escalated"),
    urgency: str | None = Query(None, description="critical | high | medium | low"),
    entity_id: str | None = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    ctx=Depends(require_api_key("read")),
    db: AsyncSession = Depends(get_db),
):
    org_id = UUID(ctx.org_id)
    repo = RecommendationRepository(db)
    offset = (page - 1) * limit
    recs = await repo.list_by_org(
        org_id, urgency=urgency, status=status, entity_id=entity_id, limit=limit, offset=offset
    )
    total = await repo.count_by_org(org_id, urgency=urgency, status=status, entity_id=entity_id)
    return envelope(
        {"recommendations": [_rec_dict(r) for r in recs], "total": total, "page": page, "limit": limit},
        ctx.org_id,
    )


@router.get("/{recommendation_id}", summary="Get recommendation")
async def get_recommendation(
    recommendation_id: str,
    ctx=Depends(require_api_key("read")),
    db: AsyncSession = Depends(get_db),
):
    org_id = UUID(ctx.org_id)
    rec = await RecommendationRepository(db).get_by_id(_rid(recommendation_id))
    if not rec or rec.org_id != org_id:
        raise not_found("Recommendation not found")
    return envelope(_rec_dict(rec), ctx.org_id)


@router.post(
    "/{recommendation_id}/action",
    summary="Mark recommendation as actioned",
    description="Marks a recommendation as actioned. Requires a write-scoped API key.",
)
async def action_recommendation(
    recommendation_id: str,
    body: ActionRequest = ActionRequest(),
    ctx=Depends(require_api_key("write")),
    db: AsyncSession = Depends(get_db),
):
    org_id = UUID(ctx.org_id)
    rec = await RecommendationRepository(db).get_by_id(_rid(recommendation_id))
    if not rec or rec.org_id != org_id:
        raise not_found("Recommendation not found")
    rec.status = "actioned"
    rec.actioned_at = datetime.now(timezone.utc)
    if body.outcome_notes:
        rec.outcome_notes = body.outcome_notes
    await db.commit()
    await db.refresh(rec)
    return envelope(_rec_dict(rec), ctx.org_id)


@router.post(
    "/{recommendation_id}/dismiss",
    summary="Dismiss recommendation",
    description="Dismisses a recommendation. Requires a write-scoped API key.",
)
async def dismiss_recommendation(
    recommendation_id: str,
    body: DismissRequest = DismissRequest(),
    ctx=Depends(require_api_key("write")),
    db: AsyncSession = Depends(get_db),
):
    org_id = UUID(ctx.org_id)
    rec = await RecommendationRepository(db).get_by_id(_rid(recommendation_id))
    if not rec or rec.org_id != org_id:
        raise not_found("Recommendation not found")
    rec.status = "dismissed"
    await db.commit()
    await db.refresh(rec)
    return envelope(_rec_dict(rec), ctx.org_id)
