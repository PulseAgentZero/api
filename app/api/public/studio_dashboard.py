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

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import not_found, rate_limited
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

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/studio", tags=["Studio"])

_PUBLIC_RL_LIMIT = 60


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
                    param_values=param_values or {},
                    page=1, page_size=500, redis=redis,
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
        visualizations=viz_responses,
    )


@router.get("/dashboards/{slug}", response_model=PublicDashboardResponse, summary="View public dashboard by slug")
async def get_public_dashboard(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PublicDashboardResponse:
    """View a public dashboard by its shareable slug. No authentication required.

    **Query params:** Any dashboard filter values can be passed as query parameters.
    Example: `?start_date=2025-01-01&region=US` — these are forwarded to all charts.

    **Rate limit:** 60 requests/minute per IP address.

    **Errors:**
    - 404 — dashboard not found or is not public.
    - 429 RATE_LIMITED — too many requests from this IP.
    """
    redis = await get_redis()
    await _apply_rate_limit(request, redis)

    dashboard = await StudioDashboardRepository(db).get_by_slug(slug)
    if not dashboard:
        raise not_found("Dashboard not found or is not public")

    param_values = dict(request.query_params)
    return await _render_dashboard(dashboard, db, redis, param_values=param_values)


@router.get("/embed/{token}", response_model=PublicDashboardResponse, summary="View private dashboard via embed token")
async def get_embed_dashboard(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PublicDashboardResponse:
    """View a private dashboard using a time-limited embed token. No authentication required.

    Tokens are generated via `POST /api/v1/studio/dashboards/{id}/embed-token`
    and are valid for the configured duration (default 24h, max 30 days).

    Intended for `<iframe>` embedding in internal tools and external portals.

    **Query params:** Dashboard filter values can be passed as query parameters.

    **Rate limit:** 60 requests/minute per IP.

    **Errors:**
    - 404 — token not found, expired, or dashboard was deleted.
    - 429 RATE_LIMITED — too many requests from this IP.
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
        from uuid import UUID
        dashboard_id = UUID(payload["dashboard_id"])
    except (KeyError, ValueError) as exc:
        raise not_found("Invalid embed token") from exc

    dashboard: StudioDashboard | None = await db.get(StudioDashboard, dashboard_id)
    if not dashboard:
        raise not_found("Dashboard not found")

    param_values = dict(request.query_params)
    return await _render_dashboard(dashboard, db, redis, param_values=param_values)
