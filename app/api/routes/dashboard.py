import logging

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.infrastructure.database.models.entity_profile import EntityProfile
from app.infrastructure.database.models.pipeline_run import PipelineRun
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.recommendation_repository import (
    RecommendationRepository,
)
from app.infrastructure.database.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/overview")
async def get_overview(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org_id = current_user.org_id
    total = await db.scalar(
        select(func.count())
        .select_from(EntityProfile)
        .where(EntityProfile.org_id == org_id, EntityProfile.is_latest.is_(True))
    )
    rd = await db.execute(
        select(EntityProfile.risk_tier, func.count())
        .where(EntityProfile.org_id == org_id, EntityProfile.is_latest.is_(True))
        .group_by(EntityProfile.risk_tier)
    )
    dist = {"High": 0, "Medium": 0, "Low": 0, "Healthy": 0}
    for tier, c in rd.all():
        if tier in dist:
            dist[tier] = int(c)

    topq = await db.execute(
        select(EntityProfile)
        .where(EntityProfile.org_id == org_id, EntityProfile.is_latest.is_(True))
        .order_by(EntityProfile.risk_score.desc().nullslast())
        .limit(5)
    )
    top_at_risk = [
        {
            "entity_id": p.entity_id,
            "entity_name": p.entity_name,
            "risk_score": float(p.risk_score or 0),
            "risk_tier": p.risk_tier,
            "segment": p.segment,
        }
        for p in topq.scalars().all()
    ]

    repo = RecommendationRepository(db)
    open_recs = await repo.count_by_org(org_id, status="open")
    crit = await repo.count_by_org(org_id, status="open", urgency="critical")

    last_run = await db.execute(
        select(PipelineRun)
        .where(PipelineRun.org_id == org_id)
        .order_by(PipelineRun.created_at.desc())
        .limit(1)
    )
    lr = last_run.scalar_one_or_none()
    last_pipeline = None
    if lr:
        last_pipeline = {
            "id": str(lr.id),
            "status": lr.status,
            "completed_at": lr.completed_at.isoformat() if lr.completed_at else None,
            "entities_scored": lr.entities_scored,
        }

    return {
        "total_entities": int(total or 0),
        "risk_distribution": dist,
        "top_at_risk": top_at_risk,
        "active_recommendations": open_recs,
        "critical_recommendations": crit,
        "last_pipeline_run": last_pipeline,
    }
