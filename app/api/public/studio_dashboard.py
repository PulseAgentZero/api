"""Public (unauthenticated) studio dashboard endpoints.

Two access methods:
1. Slug — dashboard must have is_public=True.
2. Embed token — any dashboard with a valid short-lived Redis token.

Both endpoints apply per-IP rate limiting (60 req/min) when Redis is available.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import not_found, rate_limited
from app.api.public.schemas import PublicErrorResponse
from app.api.schemas.studio import (
    PublicDashboardResponse,
    PublicVisualizationResponse,
    StudioQueryResultResponse,
)
from app.infrastructure.database.models.studio_dashboard import StudioDashboard
from app.infrastructure.database.models.studio_query import StudioQuery
from app.infrastructure.database.models.studio_visualization import StudioVisualization
from app.infrastructure.database.repositories.studio_dashboard_item_repository import (
    StudioDashboardItemRepository,
)
from app.infrastructure.database.repositories.studio_dashboard_repository import (
    StudioDashboardRepository,
)
from app.infrastructure.database.session import get_db
from app.infrastructure.redis.client import get_redis
from app.infrastructure.redis.keys import studio_embed, studio_public_rl
from app.services.studio_query_service import execute_studio_query
from app.services.studio_time_range import merge_dashboard_param_values

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/studio", tags=["Studio"])

_PUBLIC_RL_LIMIT = 60

_STUDIO_ERRORS = {
    404: {"model": PublicErrorResponse, "description": "Dashboard or token not found"},
    429: {"model": PublicErrorResponse, "description": "Per-IP rate limit exceeded (60/min)"},
}


async def _apply_rate_limit(request: Request, redis) -> None:
    """Enforce 60 req/min per IP. No-op when Redis is unavailable."""
    if redis is None:
        return
    client_ip = request.client.host if request.client else "unknown"
    rl_key = studio_public_rl(client_ip)
    try:
        n = await redis.incr(rl_key)
        if n == 1:
            await redis.expire(rl_key, 60)
        if n > _PUBLIC_RL_LIMIT:
            raise rate_limited(
                f"Public dashboard rate limit exceeded ({_PUBLIC_RL_LIMIT} req/min per IP)"
            )
    except Exception as exc:
        if hasattr(exc, "status_code"):
            raise
        logger.warning("Studio public rate-limit Redis error: %s", exc)


def _time_range_from_params(
    dashboard: StudioDashboard,
    param_values: dict,
) -> tuple[dict, dict]:
    """Extract time_range overrides from query params; return (time_range, remaining filters)."""
    filters = dict(param_values)
    time_range = dict(dashboard.time_range or {})
    preset = filters.pop("time_preset", None)
    if preset:
        time_range["preset"] = preset
    if "time_from" in filters:
        time_range["from"] = filters.pop("time_from")
    if "time_to" in filters:
        time_range["to"] = filters.pop("time_to")
    return time_range, filters


async def _render_dashboard(
    dashboard: StudioDashboard,
    db: AsyncSession,
    redis,
    param_values: dict | None = None,
) -> PublicDashboardResponse:
    """Execute all visualization queries and assemble the public dashboard response."""
    items = await StudioDashboardItemRepository(db).list_by_dashboard(
        dashboard.id, dashboard.org_id
    )
    from app.api.dependencies.plan_gate import get_org_plan

    org_plan = await get_org_plan(db, dashboard.org_id)
    time_range, filters = _time_range_from_params(dashboard, param_values or {})
    merged_params = merge_dashboard_param_values(filters, time_range)
    viz_responses: list[PublicVisualizationResponse] = []

    for item in items:
        if item.panel_type != "visualization" or not item.visualization_id:
            continue
        viz: StudioVisualization | None = await db.get(StudioVisualization, item.visualization_id)
        if not viz:
            continue
        q: StudioQuery | None = await db.get(StudioQuery, viz.query_id)
        query_result: StudioQueryResultResponse | None = None

        if q:
            try:
                raw = await execute_studio_query(
                    db, dashboard.org_id, q.connection_id, q.sql_text,
                    param_defs=q.params or [],
                    param_values=merged_params,
                    page=1, page_size=500, redis=redis, org_plan=org_plan,
                )
                query_result = StudioQueryResultResponse(**raw)
            except Exception:
                logger.warning("Public dashboard query failed for viz=%s", viz.id, exc_info=True)

        viz_responses.append(
            PublicVisualizationResponse(
                id=viz.id,
                name=viz.name,
                chart_type=viz.chart_type,
                config=viz.config,
                column_formats=viz.column_formats,
                query_result=query_result,
            )
        )

    return PublicDashboardResponse(
        id=dashboard.id,
        name=dashboard.name,
        description=dashboard.description,
        slug=dashboard.slug or "",
        layout=dashboard.layout,
        dashboard_params=dashboard.dashboard_params,
        refresh_interval_seconds=dashboard.refresh_interval_seconds,
        time_range=dashboard.time_range or {},
        visualizations=viz_responses,
    )


@router.get(
    "/dashboards/{slug}",
    response_model=PublicDashboardResponse,
    summary="View public dashboard by slug",
    response_description="Rendered dashboard with chart data (no API key).",
    responses=_STUDIO_ERRORS,
    openapi_extra={"security": []},
)
async def get_public_dashboard(
    slug: Annotated[
        str,
        Path(
            description="Shareable slug from Studio (set when `is_public=true` on the dashboard).",
            examples=["q4-churn-overview"],
        ),
    ],
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PublicDashboardResponse:
    """
    Load a **public** Pulse Studio dashboard by its slug. **No `X-API-Key` header required.**

    Visitors and embedded iframes use this URL. The dashboard owner must have enabled
    **Make public** in Studio, which assigns the slug.

    ### Dashboard filters

    Pass filter values as **query parameters** — they are forwarded to every chart query.
    Parameter names match `dashboard_params[].name` from the dashboard definition.

    Example: `GET /v1/studio/dashboards/q4-churn?region=US&start_date=2025-01-01`

    ### Rate limit

    **60 requests / minute / IP** when Redis is configured.

    ### Errors

    - **404** — unknown slug, or dashboard exists but is not public.
    - **429** — rate limit exceeded.
    """
    redis = await get_redis()
    await _apply_rate_limit(request, redis)

    dashboard = await StudioDashboardRepository(db).get_by_slug(slug)
    if not dashboard:
        raise not_found("Dashboard not found or is not public")

    param_values = dict(request.query_params)
    return await _render_dashboard(dashboard, db, redis, param_values=param_values)


@router.get(
    "/embed/{token}",
    response_model=PublicDashboardResponse,
    summary="View dashboard via embed token",
    response_description="Rendered private dashboard using a time-limited token (no API key).",
    responses=_STUDIO_ERRORS,
    openapi_extra={"security": []},
)
async def get_embed_dashboard(
    token: Annotated[
        str,
        Path(
            description="Opaque embed token from `POST /api/v1/studio/dashboards/{id}/embed-token`.",
        ),
    ],
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PublicDashboardResponse:
    """
    Load a **private** dashboard using a short-lived embed token. **No API key required.**

    Tokens are created by authenticated Studio users via:

    `POST /api/v1/studio/dashboards/{dashboard_id}/embed-token`

    Default TTL is **24 hours** (configurable up to 30 days). Use the returned URL in an
    `<iframe src="https://your-api/api/public/v1/studio/embed/{token}">`.

    ### Dashboard filters

    Same as the slug endpoint — pass filter names as query parameters.

    ### Rate limit

    **60 requests / minute / IP** when Redis is configured.

    ### Errors

    - **404** — token missing, expired, invalid, or dashboard deleted. Also returned when Redis is not configured.
    - **429** — rate limit exceeded.
    """
    redis = await get_redis()
    if redis is None:
        raise not_found("Embed token service unavailable — Redis is not configured")

    await _apply_rate_limit(request, redis)

    raw = await redis.get(studio_embed(token))
    if not raw:
        raise not_found("Embed token not found or has expired")

    try:
        payload = json.loads(raw)
        expires_at = datetime.fromisoformat(payload["expires_at"])
        if expires_at < datetime.now(timezone.utc):
            raise not_found("Embed token has expired")
        dashboard_id = UUID(payload["dashboard_id"])
    except (KeyError, ValueError) as exc:
        raise not_found("Invalid embed token") from exc

    dashboard: StudioDashboard | None = await db.get(StudioDashboard, dashboard_id)
    if not dashboard:
        raise not_found("Dashboard not found")

    param_values = dict(request.query_params)
    return await _render_dashboard(dashboard, db, redis, param_values=param_values)
