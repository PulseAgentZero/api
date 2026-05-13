"""SQL-backed analytics aggregations for internal /analytics routes."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import distinct, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models.entity_profile import EntityProfile
from app.infrastructure.database.models.entity_risk_history import EntityRiskHistory
from app.infrastructure.database.models.pipeline_run import PipelineRun


async def count_improved_worsened(
    db: AsyncSession, org_id: UUID, since: datetime
) -> tuple[int, int]:
    """Entities with at least two risk history rows in the window: score went down vs up."""
    q = text(
        """
        WITH bounds AS (
            SELECT entity_id,
                   (array_agg(risk_score ORDER BY recorded_at ASC))[1] AS first_in_period,
                   (array_agg(risk_score ORDER BY recorded_at DESC))[1] AS last_in_period,
                   COUNT(*) AS n
            FROM entity_risk_history
            WHERE org_id = CAST(:org_id AS uuid) AND recorded_at >= :since
            GROUP BY entity_id
            HAVING COUNT(*) >= 2
        )
        SELECT
            COALESCE(SUM(CASE WHEN last_in_period < first_in_period THEN 1 ELSE 0 END), 0)::int AS improved,
            COALESCE(SUM(CASE WHEN last_in_period > first_in_period THEN 1 ELSE 0 END), 0)::int AS worsened
        FROM bounds
        """
    )
    row = (
        await db.execute(
            q,
            {"org_id": str(org_id), "since": since},
        )
    ).one()
    return int(row.improved or 0), int(row.worsened or 0)


async def risk_trend_series(
    db: AsyncSession,
    org_id: UUID,
    *,
    since: datetime,
    granularity: str,
) -> list[dict[str, Any]]:
    trunc = {"day": "day", "week": "week", "month": "month"}.get(granularity, "week")
    bucket = func.date_trunc(trunc, EntityRiskHistory.recorded_at)
    stmt = (
        select(
            bucket.label("bucket"),
            func.avg(EntityRiskHistory.risk_score).label("avg_score"),
            func.count(distinct(EntityRiskHistory.entity_id)).label("entity_rows"),
        )
        .where(EntityRiskHistory.org_id == org_id, EntityRiskHistory.recorded_at >= since)
        .group_by(bucket)
        .order_by(bucket)
    )
    rows = (await db.execute(stmt)).all()
    return [
        {
            "t": r.bucket.isoformat() if r.bucket else None,
            "avg_risk_score": float(r.avg_score or 0),
            "samples": int(r.entity_rows or 0),
        }
        for r in rows
        if r.bucket is not None
    ]


async def cohort_tier_movements(
    db: AsyncSession, org_id: UUID, since: datetime
) -> list[dict[str, Any]]:
    q = text(
        """
        WITH ordered AS (
            SELECT entity_id,
                   risk_tier,
                   LAG(risk_tier) OVER (PARTITION BY entity_id ORDER BY recorded_at) AS prev_tier
            FROM entity_risk_history
            WHERE org_id = CAST(:org_id AS uuid) AND recorded_at >= :since
        )
        SELECT prev_tier, risk_tier AS to_tier, COUNT(*)::int AS cnt
        FROM ordered
        WHERE prev_tier IS NOT NULL AND prev_tier <> risk_tier
        GROUP BY prev_tier, risk_tier
        ORDER BY cnt DESC
        """
    )
    rows = (await db.execute(q, {"org_id": str(org_id), "since": since})).all()
    return [
        {"from_tier": r.prev_tier, "to_tier": r.to_tier, "count": int(r.cnt)}
        for r in rows
    ]


async def pipeline_performance_stats(
    db: AsyncSession, org_id: UUID
) -> dict[str, Any]:
    """Averages over completed runs with metrics recorded."""
    done = (
        PipelineRun.org_id == org_id,
        PipelineRun.completed_at.isnot(None),
    )
    avg_dur = await db.scalar(
        select(func.avg(PipelineRun.duration_ms)).where(
            *done,
            PipelineRun.duration_ms.isnot(None),
        )
    )
    avg_ent = await db.scalar(
        select(func.avg(PipelineRun.entities_scored)).where(
            PipelineRun.org_id == org_id,
            PipelineRun.status == "succeeded",
        )
    )
    avg_tok = await db.scalar(
        select(func.avg(PipelineRun.total_tokens)).where(
            PipelineRun.org_id == org_id,
            PipelineRun.status == "succeeded",
        )
    )
    return {
        "avg_duration_ms": float(avg_dur or 0),
        "avg_entities_per_run": float(avg_ent or 0),
        "avg_tokens_per_run": float(avg_tok or 0),
    }


async def latest_entity_rows_for_export(db: AsyncSession, org_id: UUID) -> list[Any]:
    stmt = (
        select(
            EntityProfile.entity_id,
            EntityProfile.entity_name,
            EntityProfile.risk_score,
            EntityProfile.risk_tier,
            EntityProfile.segment,
        )
        .where(EntityProfile.org_id == org_id, EntityProfile.is_latest.is_(True))
        .order_by(EntityProfile.entity_id)
    )
    return list((await db.execute(stmt)).all())
