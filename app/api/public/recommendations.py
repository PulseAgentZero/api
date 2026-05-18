from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.api_key_auth import require_api_key
from app.api.errors import bad_request, not_found
from app.api.public.envelope import envelope
from app.api.public.schemas import (
    ActionRecommendationRequest,
    DismissRecommendationRequest,
    PublicErrorResponse,
    RecStatus,
    RecUrgency,
    RecommendationDetailResponse,
    RecommendationListResponse,
)
from app.infrastructure.database.repositories.recommendation_repository import RecommendationRepository
from app.infrastructure.database.session import get_db

router = APIRouter(prefix="/recommendations", tags=["Recommendations"])

_READ_ERRORS = {
    401: {"model": PublicErrorResponse, "description": "Invalid or expired API key"},
    404: {"model": PublicErrorResponse, "description": "Recommendation not found"},
    429: {"model": PublicErrorResponse, "description": "Rate limit exceeded"},
}

_WRITE_ERRORS = {
    **_READ_ERRORS,
    403: {"model": PublicErrorResponse, "description": "Write scope required"},
}


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
    response_model=RecommendationListResponse,
    summary="List recommendations",
    response_description="Paginated recommendations for your organization.",
    responses=_READ_ERRORS,
)
async def list_recommendations(
    status: Annotated[
        RecStatus | None,
        Query(description="Filter by workflow status."),
    ] = None,
    urgency: Annotated[
        RecUrgency | None,
        Query(description="Filter by urgency level."),
    ] = None,
    entity_id: Annotated[
        str | None,
        Query(description="Return only recommendations for this entity."),
    ] = None,
    page: Annotated[int, Query(ge=1, description="Page number (1-based).")] = 1,
    limit: Annotated[int, Query(ge=1, le=100, description="Results per page (max 100).")] = 50,
    ctx=Depends(require_api_key("read")),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns AI-generated recommendations for your org. Results are ordered by recency.

    Combine **`status=open`** with **`urgency=critical`** to build alerting workflows.
    """
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


@router.get(
    "/{recommendation_id}",
    response_model=RecommendationDetailResponse,
    summary="Get recommendation",
    response_description="Single recommendation with full reasoning and suggested action.",
    responses=_READ_ERRORS,
)
async def get_recommendation(
    recommendation_id: Annotated[
        str,
        Path(description="Recommendation UUID."),
    ],
    ctx=Depends(require_api_key("read")),
    db: AsyncSession = Depends(get_db),
):
    """Fetch one recommendation by ID. Returns 404 if the ID is invalid or belongs to another org."""
    org_id = UUID(ctx.org_id)
    rec = await RecommendationRepository(db).get_by_id(_rid(recommendation_id))
    if not rec or rec.org_id != org_id:
        raise not_found("Recommendation not found")
    return envelope(_rec_dict(rec), ctx.org_id)


@router.post(
    "/{recommendation_id}/action",
    response_model=RecommendationDetailResponse,
    summary="Mark recommendation as actioned",
    response_description="Updated recommendation with `status=actioned`.",
    responses=_WRITE_ERRORS,
)
async def action_recommendation(
    recommendation_id: Annotated[str, Path(description="Recommendation UUID.")],
    body: ActionRecommendationRequest = ActionRecommendationRequest(),
    ctx=Depends(require_api_key("write")),
    db: AsyncSession = Depends(get_db),
):
    """
    Marks a recommendation as **actioned** and records an optional outcome note.

    Requires a **write-scoped** API key. Idempotent only in the sense that re-posting
    on an already-actioned row will keep `status=actioned`.
    """
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
    response_model=RecommendationDetailResponse,
    summary="Dismiss recommendation",
    response_description="Updated recommendation with `status=dismissed`.",
    responses=_WRITE_ERRORS,
)
async def dismiss_recommendation(
    recommendation_id: Annotated[str, Path(description="Recommendation UUID.")],
    body: DismissRecommendationRequest = DismissRecommendationRequest(),
    ctx=Depends(require_api_key("write")),
    db: AsyncSession = Depends(get_db),
):
    """
    Dismisses a recommendation so it no longer appears in open queues.

    Requires a **write-scoped** API key.
    """
    org_id = UUID(ctx.org_id)
    rec = await RecommendationRepository(db).get_by_id(_rid(recommendation_id))
    if not rec or rec.org_id != org_id:
        raise not_found("Recommendation not found")
    rec.status = "dismissed"
    await db.commit()
    await db.refresh(rec)
    return envelope(_rec_dict(rec), ctx.org_id)
