"""Entity list/detail/history reads — shared by internal `/entities` and public API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import not_found
from app.infrastructure.database.models.entity_profile import EntityProfile
from app.infrastructure.database.models.entity_risk_history import EntityRiskHistory
from app.infrastructure.database.models.recommendation import Recommendation


async def fetch_entity_list(
    db: AsyncSession,
    org_id: UUID,
    *,
    page: int = 1,
    limit: int = 50,
    risk_tier: str | None = None,
    segment: str | None = None,
    search: str | None = None,
) -> dict:
    conds = [
        EntityProfile.org_id == org_id,
        EntityProfile.is_latest.is_(True),
    ]
    if risk_tier:
        conds.append(EntityProfile.risk_tier == risk_tier)
    if segment:
        conds.append(EntityProfile.segment == segment)
    if search:
        like = f"%{search.lower()}%"
        conds.append(
            func.lower(EntityProfile.entity_name).like(like)
            | func.lower(EntityProfile.entity_id).like(like)
        )

    total = int(await db.scalar(select(func.count()).select_from(EntityProfile).where(*conds)) or 0)
    stmt = (
        select(EntityProfile)
        .where(*conds)
        .order_by(EntityProfile.risk_score.desc().nullslast())
        .offset((page - 1) * limit)
        .limit(limit)
    )
    rows = list((await db.execute(stmt)).scalars().all())

    entities = []
    for p in rows:
        open_n = await db.scalar(
            select(func.count())
            .select_from(Recommendation)
            .where(
                Recommendation.org_id == org_id,
                Recommendation.entity_id == p.entity_id,
                Recommendation.status == "open",
            )
        )
        entities.append(
            {
                "entity_id": p.entity_id,
                "entity_name": p.entity_name,
                "segment": p.segment,
                "risk_score": float(p.risk_score or 0),
                "risk_tier": p.risk_tier,
                "risk_narrative": p.risk_narrative,
                "open_recommendations": int(open_n or 0),
                "created_at": p.created_at.isoformat(),
            }
        )

    pages = (total + limit - 1) // limit if total else 1
    return {
        "entities": entities,
        "total": total,
        "page": page,
        "limit": limit,
        "pages": pages,
    }


async def fetch_entity_detail(db: AsyncSession, org_id: UUID, entity_id: str) -> dict:
    result = await db.execute(
        select(EntityProfile)
        .where(
            EntityProfile.org_id == org_id,
            EntityProfile.entity_id == entity_id,
            EntityProfile.is_latest.is_(True),
        )
        .limit(1)
    )
    p = result.scalar_one_or_none()
    if p is None:
        raise not_found("Entity profile not found")

    recs = await db.execute(
        select(Recommendation)
        .where(Recommendation.org_id == org_id, Recommendation.entity_id == entity_id)
        .order_by(Recommendation.created_at.desc())
        .limit(20)
    )
    recommendations = [
        {
            "id": str(r.id),
            "title": r.title,
            "urgency": r.urgency,
            "status": r.status,
            "created_at": r.created_at.isoformat(),
        }
        for r in recs.scalars().all()
    ]

    return {
        "entity_id": p.entity_id,
        "entity_name": p.entity_name,
        "segment": p.segment,
        "risk_score": float(p.risk_score or 0),
        "risk_tier": p.risk_tier,
        "risk_narrative": p.risk_narrative,
        "profile_data": p.profile_data or {},
        "recommendations": recommendations,
        "last_pipeline_run_at": p.created_at.isoformat(),
    }


async def fetch_entity_risk_history(
    db: AsyncSession,
    org_id: UUID,
    entity_id: str,
    *,
    period: str = "30d",
) -> dict:
    if period not in ("7d", "30d", "90d", "180d"):
        period = "30d"
    days = {"7d": 7, "30d": 30, "90d": 90, "180d": 180}[period]
    since = datetime.now(timezone.utc) - timedelta(days=days)
    result = await db.execute(
        select(EntityRiskHistory)
        .where(
            EntityRiskHistory.org_id == org_id,
            EntityRiskHistory.entity_id == entity_id,
            EntityRiskHistory.recorded_at >= since,
        )
        .order_by(EntityRiskHistory.recorded_at.asc())
    )
    rows = list(result.scalars().all())
    return {
        "entity_id": entity_id,
        "period": period,
        "points": [
            {
                "risk_score": float(h.risk_score),
                "risk_tier": h.risk_tier,
                "recorded_at": h.recorded_at.isoformat(),
            }
            for h in rows
        ],
    }
