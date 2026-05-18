"""Studio query auto-refresh scheduler.

Runs scheduled SQL queries on their cron schedule, warming the Redis cache
so dashboards always show fresh data without waiting for on-demand execution.
"""

from __future__ import annotations

import logging
from uuid import UUID

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def _refresh_query(query_id_str: str) -> None:
    """Scheduled job: re-execute a saved query and warm the Redis cache."""
    query_id = UUID(query_id_str)
    try:
        from app.infrastructure.database.models.studio_query import StudioQuery
        from app.infrastructure.database.session import async_session_factory
        from app.infrastructure.redis.client import get_redis
        from app.services.studio_query_service import execute_studio_query

        async with async_session_factory() as session:
            q = await session.get(StudioQuery, query_id)
            if not q or not q.refresh_enabled:
                logger.debug("Studio refresh skipped for query %s (disabled or not found)", query_id)
                return

            redis = await get_redis()

            # Use default param values for unattended scheduled runs
            param_values = {
                p["name"]: p["default_value"]
                for p in (q.params or [])
                if p.get("default_value") is not None
            }

            await execute_studio_query(
                session,
                q.org_id,
                q.connection_id,
                q.sql_text,
                param_defs=q.params or [],
                param_values=param_values,
                page=1,
                page_size=_MAX_CACHE_ROWS,
                redis=redis,
            )
            logger.info("Studio auto-refresh complete for query %s (%s)", query_id, q.name)
    except Exception:
        logger.exception("Studio auto-refresh failed for query %s", query_id)


_MAX_CACHE_ROWS = 500


def schedule_query_refresh(query_id: UUID, cron_expr: str) -> None:
    """Add or update a cron refresh job for a query. No-op if scheduler not started."""
    if _scheduler is None:
        logger.debug("Studio refresh scheduler not running — skipping schedule for %s", query_id)
        return
    try:
        _scheduler.add_job(
            _refresh_query,
            trigger=CronTrigger.from_crontab(cron_expr),
            id=f"studio_refresh_{query_id}",
            args=[str(query_id)],
            replace_existing=True,
        )
        logger.info("Scheduled studio refresh for query %s (cron: %s)", query_id, cron_expr)
    except Exception:
        logger.warning("Could not schedule refresh for query %s", query_id, exc_info=True)


def unschedule_query_refresh(query_id: UUID) -> None:
    """Remove the cron refresh job for a query. No-op if not scheduled."""
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job(f"studio_refresh_{query_id}")
        logger.info("Unscheduled studio refresh for query %s", query_id)
    except Exception:
        pass  # Job may not exist — that's fine


async def start_studio_refresh_scheduler() -> AsyncIOScheduler:
    """Start APScheduler and load all enabled refresh schedules from DB."""
    global _scheduler
    _scheduler = AsyncIOScheduler()
    _scheduler.start()

    try:
        from app.infrastructure.database.models.studio_query import StudioQuery
        from app.infrastructure.database.session import async_session_factory

        async with async_session_factory() as session:
            result = await session.execute(
                select(StudioQuery).where(
                    StudioQuery.refresh_enabled.is_(True),
                    StudioQuery.refresh_cron.isnot(None),
                )
            )
            queries = result.scalars().all()

        scheduled = 0
        for q in queries:
            try:
                schedule_query_refresh(q.id, q.refresh_cron)  # type: ignore[arg-type]
                scheduled += 1
            except Exception:
                logger.warning("Could not schedule refresh for query %s", q.id, exc_info=True)

        logger.info("Studio refresh scheduler started (%d queries scheduled)", scheduled)
    except Exception:
        logger.exception("Studio refresh scheduler startup failed — scheduler is running but empty")

    return _scheduler


def shutdown_studio_refresh_scheduler() -> None:
    """Gracefully shut down the studio refresh scheduler."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        logger.info("Studio refresh scheduler shut down")
        _scheduler = None
