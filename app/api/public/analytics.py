"""Public analytics — aggregates from entity_profiles and recommendations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.api_key_auth import require_api_key
from app.api.public.envelope import envelope
from app.api.public.schemas import AnalyticsOverviewResponse, AnalyticsPeriod, PublicErrorResponse
from app.infrastructure.database.models.entity_profile import EntityProfile
from app.infrastructure.database.models.pipeline_run import PipelineRun
from app.infrastructure.database.models.recommendation import Recommendation
from app.infrastructure.database.session import get_db

router = APIRouter(prefix="/analytics", tags=["Analytics"])

_READ_ERRORS = {
    401: {"model": PublicErrorResponse, "description": "Invalid or expired API key"},
    429: {"model": PublicErrorResponse, "description": "Rate limit exceeded"},
}


@router.get(
    "/overview",
    response_model=AnalyticsOverviewResponse,
    summary="Analytics overview",
    response_description="Org-level risk and activity aggregates for the selected period.",
    responses=_READ_ERRORS,
)
async def get_overview(
    period: Annotated[
        AnalyticsPeriod,
        Query(description="Rolling window for pipeline run counts."),
    ] = "30d",
    ctx=Depends(require_api_key("read")),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns a snapshot of your organization's intelligence layer:

    - **total_entities** — latest profiled entities
    - **risk_distribution** — counts per tier (High / Medium / Low / Healthy)
    - **average_risk_score** — mean score across latest profiles
    - **open_recommendations** — recommendations with `status=open`
    - **pipeline_runs_in_period** — runs started within the lookback window
    """
    org_id = UUID(ctx.org_id)
    days = {"7d": 7, "30d": 30, "90d": 90}.get(period, 30)
    since = datetime.now(timezone.utc) - timedelta(days=days)

    total_entities = int(
        await db.scalar(
            select(func.count())
            .select_from(EntityProfile)
            .where(EntityProfile.org_id == org_id, EntityProfile.is_latest.is_(True))
        )
        or 0
    )

    rd = await db.execute(
        select(EntityProfile.risk_tier, func.count())
        .where(EntityProfile.org_id == org_id, EntityProfile.is_latest.is_(True))
        .group_by(EntityProfile.risk_tier)
    )
    risk_distribution = {"High": 0, "Medium": 0, "Low": 0, "Healthy": 0}
    for tier, c in rd.all():
        if tier in risk_distribution:
            risk_distribution[tier] = int(c)

    avg_score = await db.scalar(
        select(func.avg(EntityProfile.risk_score)).where(
            EntityProfile.org_id == org_id,
            EntityProfile.is_latest.is_(True),
        )
    )
    open_recs = int(
        await db.scalar(
            select(func.count())
            .select_from(Recommendation)
            .where(Recommendation.org_id == org_id, Recommendation.status == "open")
        )
        or 0
    )

    runs_in_period = int(
        await db.scalar(
            select(func.count())
            .select_from(PipelineRun)
            .where(PipelineRun.org_id == org_id, PipelineRun.created_at >= since)
        )
        or 0
    )

    data = {
        "period": period,
        "total_entities": total_entities,
        "risk_distribution": risk_distribution,
        "average_risk_score": float(avg_score) if avg_score is not None else None,
        "open_recommendations": open_recs,
        "pipeline_runs_in_period": runs_in_period,
    }
    return envelope(data, ctx.org_id)
