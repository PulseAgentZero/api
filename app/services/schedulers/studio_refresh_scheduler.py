"""Studio query auto-refresh scheduler.

Runs scheduled SQL queries on their cron schedule, warming the Redis cache
so dashboards always show fresh data without waiting for on-demand execution.

Schedule changes are persisted on StudioQuery rows; the scheduler process
syncs jobs from the DB periodically (API does not call APScheduler in-process).
"""

from __future__ import annotations

import logging
import os
from uuid import UUID

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None

STUDIO_REFRESH_SYNC_INTERVAL_MINUTES = int(
    os.getenv("STUDIO_REFRESH_SYNC_INTERVAL_MINUTES", "5")
)

_MAX_CACHE_ROWS = 500


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

            from app.api.dependencies.plan_gate import get_org_plan

            org_plan = await get_org_plan(session, q.org_id)
            redis = await get_redis()

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
                org_plan=org_plan,
            )
            logger.info("Studio auto-refresh complete for query %s (%s)", query_id, q.name)
    except Exception:
        logger.exception("Studio auto-refresh failed for query %s", query_id)


def schedule_query_refresh(query_id: UUID, cron_expr: str) -> None:
    """Add or update a cron refresh job for a query (scheduler process only)."""
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
        logger.debug("Scheduled studio refresh for query %s (cron: %s)", query_id, cron_expr)
    except Exception:
        logger.warning("Could not schedule refresh for query %s", query_id, exc_info=True)


def unschedule_query_refresh(query_id: UUID) -> None:
    """Remove the cron refresh job for a query (scheduler process only)."""
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job(f"studio_refresh_{query_id}")
        logger.debug("Unscheduled studio refresh for query %s", query_id)
    except Exception:
        pass


async def sync_refresh_jobs_from_db() -> None:
    """Reconcile APScheduler jobs with refresh_enabled queries in the database."""
    if _scheduler is None:
        return

    from app.infrastructure.database.models.studio_query import StudioQuery
    from app.infrastructure.database.session import async_session_factory

    try:
        async with async_session_factory() as session:
            result = await session.execute(
                select(StudioQuery.id, StudioQuery.refresh_cron).where(
                    StudioQuery.refresh_enabled.is_(True),
                    StudioQuery.refresh_cron.isnot(None),
                )
            )
            rows = list(result.all())

        desired_ids = {row[0] for row in rows}
        for query_id, cron_expr in rows:
            if cron_expr:
                schedule_query_refresh(query_id, cron_expr)

        stale_jobs = [
            job.id
            for job in _scheduler.get_jobs()
            if isinstance(job.id, str)
            and job.id.startswith("studio_refresh_")
            and job.id != "studio_refresh_sync"
        ]
        for job_id in stale_jobs:
            try:
                qid = UUID(job_id.removeprefix("studio_refresh_"))
            except ValueError:
                continue
            if qid not in desired_ids:
                try:
                    _scheduler.remove_job(job_id)
                    logger.info("Removed stale studio refresh job %s", job_id)
                except Exception:
                    pass

        logger.debug("Studio refresh sync: %d active query schedules", len(desired_ids))
    except Exception:
        logger.exception("Studio refresh sync from DB failed")


async def start_studio_refresh_scheduler() -> AsyncIOScheduler:
    """Start APScheduler, load refresh jobs from DB, and schedule periodic sync."""
    global _scheduler
    _scheduler = AsyncIOScheduler()
    _scheduler.start()

    await sync_refresh_jobs_from_db()

    minutes = max(1, STUDIO_REFRESH_SYNC_INTERVAL_MINUTES)
    _scheduler.add_job(
        sync_refresh_jobs_from_db,
        trigger=IntervalTrigger(minutes=minutes),
        id="studio_refresh_sync",
        replace_existing=True,
    )

    job_count = sum(
        1
        for j in _scheduler.get_jobs()
        if isinstance(j.id, str) and j.id.startswith("studio_refresh_") and j.id != "studio_refresh_sync"
    )
    logger.info(
        "Studio refresh scheduler started (%d queries, sync every %dm)",
        job_count,
        minutes,
    )

    return _scheduler


def shutdown_studio_refresh_scheduler() -> None:
    """Gracefully shut down the studio refresh scheduler."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        logger.info("Studio refresh scheduler shut down")
        _scheduler = None
