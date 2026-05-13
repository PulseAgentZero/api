"""Analytics endpoints (BACKEND_ROUTES §11)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.dependencies.plan_gate import require_feature
from app.infrastructure.database.models.entity_profile import EntityProfile
from app.infrastructure.database.models.pipeline_run import PipelineRun
from app.infrastructure.database.models.recommendation import Recommendation
from app.infrastructure.database.models.user import User
from app.infrastructure.database.session import get_db

router = APIRouter(prefix="/analytics", tags=["Analytics"])


@router.get("/overview")
async def analytics_overview(
    period: str = Query("30d", pattern="^(7d|30d|90d)$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_feature(db, current_user.org_id, "advanced_analytics")
    org_id = current_user.org_id
    days = {"7d": 7, "30d": 30, "90d": 90}[period]
    since = datetime.now(timezone.utc) - timedelta(days=days)

    n_entities = await db.scalar(
        select(func.count())
        .select_from(EntityProfile)
        .where(EntityProfile.org_id == org_id, EntityProfile.is_latest.is_(True))
    )
    runs = await db.scalar(
        select(func.count())
        .select_from(PipelineRun)
        .where(PipelineRun.org_id == org_id, PipelineRun.created_at >= since)
    )
    rec_actioned = await db.scalar(
        select(func.count())
        .select_from(Recommendation)
        .where(
            Recommendation.org_id == org_id,
            Recommendation.status == "actioned",
            Recommendation.updated_at >= since,
        )
    )
    rec_total = await db.scalar(
        select(func.count()).select_from(Recommendation).where(Recommendation.org_id == org_id)
    )
    acceptance = (
        float(rec_actioned or 0) / float(rec_total or 1) if rec_total else 0.0
    )

    rd = await db.execute(
        select(EntityProfile.risk_tier, func.count())
        .where(EntityProfile.org_id == org_id, EntityProfile.is_latest.is_(True))
        .group_by(EntityProfile.risk_tier)
    )
    dist: dict[str, int] = {"High": 0, "Medium": 0, "Low": 0, "Healthy": 0}
    for tier, c in rd.all():
        if tier in dist:
            dist[tier] = int(c)

    avg_score = await db.scalar(
        select(func.avg(EntityProfile.risk_score)).where(
            EntityProfile.org_id == org_id, EntityProfile.is_latest.is_(True)
        )
    )

    return {
        "period": period,
        "avg_risk_score": float(avg_score or 0),
        "entities_improved": 0,
        "entities_worsened": 0,
        "recommendation_acceptance_rate": round(acceptance, 4),
        "risk_distribution": dist,
        "pipeline_runs_count": int(runs or 0),
        "total_entities_profiled": int(n_entities or 0),
    }


@router.get("/risk-trend")
async def analytics_risk_trend(
    period: str = Query("90d"),
    granularity: str = Query("week", pattern="^(day|week|month)$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_feature(db, current_user.org_id, "advanced_analytics")
    return {"granularity": granularity, "series": []}


@router.get("/segments")
async def analytics_segments(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_feature(db, current_user.org_id, "advanced_analytics")
    org_id = current_user.org_id
    q = await db.execute(
        select(EntityProfile.segment, func.count(), func.avg(EntityProfile.risk_score))
        .where(EntityProfile.org_id == org_id, EntityProfile.is_latest.is_(True))
        .group_by(EntityProfile.segment)
    )
    segments = []
    for seg, cnt, avg in q.all():
        if seg is None:
            continue
        rd = await db.execute(
            select(EntityProfile.risk_tier, func.count())
            .where(
                EntityProfile.org_id == org_id,
                EntityProfile.is_latest.is_(True),
                EntityProfile.segment == seg,
            )
            .group_by(EntityProfile.risk_tier)
        )
        d = {"High": 0, "Medium": 0, "Low": 0, "Healthy": 0}
        for t, c in rd.all():
            if t in d:
                d[t] = int(c)
        segments.append(
            {
                "segment": seg,
                "entity_count": int(cnt),
                "avg_risk_score": float(avg or 0),
                "risk_distribution": d,
            }
        )
    return {"segments": segments}


@router.get("/cohorts")
async def analytics_cohorts(
    period: str = Query("30d"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_feature(db, current_user.org_id, "advanced_analytics")
    return {"period": period, "movements": []}


@router.get("/pipeline-performance")
async def analytics_pipeline_performance(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_feature(db, current_user.org_id, "advanced_analytics")
    org_id = current_user.org_id
    rows = await db.execute(
        select(PipelineRun.status, func.count()).where(PipelineRun.org_id == org_id).group_by(PipelineRun.status)
    )
    by_status: dict[str, int] = {}
    for st, c in rows.all():
        by_status[st] = int(c)
    total = sum(by_status.values())
    succeeded = by_status.get("succeeded", 0)
    return {
        "avg_duration_ms": 0,
        "avg_entities_per_run": 0,
        "total_runs": total,
        "success_rate": (succeeded / total) if total else 0.0,
        "avg_tokens_per_run": 0,
        "runs_by_status": by_status,
    }


@router.post("/export")
async def analytics_export(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_feature(db, current_user.org_id, "advanced_analytics")
    return {"export_id": str(uuid.uuid4()), "status": "processing"}


@router.get("/exports/{export_id}")
async def analytics_export_status(
    export_id: str,
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    return {
        "export_id": export_id,
        "status": "failed",
        "download_url": None,
        "expires_at": None,
    }
