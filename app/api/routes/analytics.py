"""Analytics endpoints (BACKEND_ROUTES §11)."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from io import StringIO
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.dependencies.plan_gate import require_feature
from app.api.errors import bad_request, not_found
from app.infrastructure.database.models.analytics_export import AnalyticsExport
from app.infrastructure.database.models.entity_profile import EntityProfile
from app.infrastructure.database.models.pipeline_run import PipelineRun
from app.infrastructure.database.models.recommendation import Recommendation
from app.infrastructure.database.models.user import User
from app.infrastructure.database.session import get_db
from app.services.analytics_insights import (
    cohort_tier_movements,
    count_improved_worsened,
    latest_entity_rows_for_export,
    pipeline_performance_stats,
    risk_trend_series,
)

router = APIRouter(prefix="/analytics", tags=["Analytics"])


def _period_days(period: str) -> int:
    return {"7d": 7, "30d": 30, "90d": 90}.get(period, 30)


@router.get("/overview")
async def analytics_overview(
    period: str = Query("30d", pattern="^(7d|30d|90d)$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_feature(db, current_user.org_id, "advanced_analytics")
    org_id = current_user.org_id
    since = datetime.now(timezone.utc) - timedelta(days=_period_days(period))

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
    acceptance = float(rec_actioned or 0) / float(rec_total or 1) if rec_total else 0.0

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

    improved, worsened = await count_improved_worsened(db, org_id, since)

    return {
        "period": period,
        "avg_risk_score": float(avg_score or 0),
        "entities_improved": improved,
        "entities_worsened": worsened,
        "recommendation_acceptance_rate": round(acceptance, 4),
        "risk_distribution": dist,
        "pipeline_runs_count": int(runs or 0),
        "total_entities_profiled": int(n_entities or 0),
    }


@router.get("/risk-trend")
async def analytics_risk_trend(
    period: str = Query("90d", pattern="^(7d|30d|90d)$"),
    granularity: str = Query("week", pattern="^(day|week|month)$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_feature(db, current_user.org_id, "advanced_analytics")
    org_id = current_user.org_id
    since = datetime.now(timezone.utc) - timedelta(days=_period_days(period))
    series = await risk_trend_series(db, org_id, since=since, granularity=granularity)
    return {"period": period, "granularity": granularity, "series": series}


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
    period: str = Query("30d", pattern="^(7d|30d|90d)$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_feature(db, current_user.org_id, "advanced_analytics")
    org_id = current_user.org_id
    since = datetime.now(timezone.utc) - timedelta(days=_period_days(period))
    movements = await cohort_tier_movements(db, org_id, since)
    return {"period": period, "movements": movements}


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
    perf = await pipeline_performance_stats(db, org_id)
    return {
        "avg_duration_ms": perf["avg_duration_ms"],
        "avg_entities_per_run": perf["avg_entities_per_run"],
        "total_runs": total,
        "success_rate": (succeeded / total) if total else 0.0,
        "avg_tokens_per_run": perf["avg_tokens_per_run"],
        "runs_by_status": by_status,
    }


@router.post("/export")
async def analytics_export(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_feature(db, current_user.org_id, "advanced_analytics")
    row = AnalyticsExport(org_id=current_user.org_id, status="processing", format="csv")
    db.add(row)
    await db.flush()
    try:
        buf = StringIO()
        w = csv.writer(buf)
        w.writerow(["entity_id", "entity_name", "risk_score", "risk_tier", "segment"])
        for r in await latest_entity_rows_for_export(db, current_user.org_id):
            w.writerow(
                [
                    r.entity_id,
                    r.entity_name or "",
                    float(r.risk_score) if r.risk_score is not None else "",
                    r.risk_tier or "",
                    r.segment or "",
                ]
            )
        raw = buf.getvalue().encode("utf-8")
        if len(raw) > 15 * 1024 * 1024:
            raise ValueError("Export exceeds 15 MiB limit")
        row.content = raw
        row.status = "completed"
        row.completed_at = datetime.now(timezone.utc)
    except Exception as e:
        row.status = "failed"
        row.error_message = str(e)[:2000]
    await db.commit()
    return {"export_id": str(row.id), "status": row.status}


@router.get("/exports/{export_id}/download")
async def analytics_export_download(
    export_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await require_feature(db, current_user.org_id, "advanced_analytics")
    try:
        eid = UUID(export_id)
    except ValueError as exc:
        raise bad_request("BAD_REQUEST", "Invalid export_id") from exc
    exp = await db.get(AnalyticsExport, eid)
    if not exp or exp.org_id != current_user.org_id:
        raise not_found()
    if exp.status != "completed" or not exp.content:
        raise bad_request("BAD_REQUEST", "Export is not ready for download")
    return Response(
        content=exp.content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="pulse-analytics-{export_id}.csv"'},
    )


@router.get("/exports/{export_id}")
async def analytics_export_status(
    export_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_feature(db, current_user.org_id, "advanced_analytics")
    try:
        eid = UUID(export_id)
    except ValueError as exc:
        raise bad_request("BAD_REQUEST", "Invalid export_id") from exc
    exp = await db.get(AnalyticsExport, eid)
    if not exp or exp.org_id != current_user.org_id:
        raise not_found()
    download_url = None
    expires_at = None
    if exp.status == "completed" and exp.content:
        download_url = f"/api/v1/analytics/exports/{export_id}/download"
    return {
        "export_id": export_id,
        "status": exp.status,
        "error_message": exp.error_message,
        "download_url": download_url,
        "expires_at": expires_at,
        "created_at": exp.created_at.isoformat(),
        "completed_at": exp.completed_at.isoformat() if exp.completed_at else None,
    }
