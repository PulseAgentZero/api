"""Monthly usage rollover scheduler.

Runs on the 1st of every month at 00:05 UTC.

Jobs:
  1. Write a monthly_usage_summary UsageEvent for each org (audit trail of
     what usage looked like before the new month started).
  2. Delete UsageEvent rows older than 6 months (data hygiene).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import delete, select

from app.config.settings import settings
from app.infrastructure.database.models.organization import Organization
from app.infrastructure.database.models.usage_event import UsageEvent
from app.infrastructure.database.session import async_session_factory

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def _monthly_rollover() -> None:
    """Snapshot each org's prior-month usage into UsageEvent and clean old rows."""
    if not settings.is_database_configured():
        return

    from app.api.dependencies.plan_gate import get_usage_summary

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=180)  # keep 6 months of events

    logger.info("Usage reset scheduler: starting monthly rollover for %s", now.strftime("%Y-%m"))

    try:
        async with async_session_factory() as session:
            org_ids = list(
                (await session.execute(select(Organization.id))).scalars().all()
            )

        processed = 0
        for org_id in org_ids:
            try:
                async with async_session_factory() as session:
                    summary = await get_usage_summary(session, org_id)
                    event = UsageEvent(
                        org_id=org_id,
                        event_type="monthly_usage_summary",
                        quantity=1,
                        metadata_={
                            "month": now.strftime("%Y-%m"),
                            "plan": summary["plan"],
                            "usage": summary["limits"],
                        },
                    )
                    session.add(event)
                    await session.commit()
                    processed += 1
            except Exception:
                logger.exception("Failed to snapshot usage for org %s", org_id)

        # Clean up old usage events
        async with async_session_factory() as session:
            result = await session.execute(
                delete(UsageEvent).where(UsageEvent.recorded_at < cutoff)
            )
            deleted = result.rowcount
            await session.commit()

        logger.info(
            "Usage reset scheduler: snapshotted %d orgs, deleted %d old events",
            processed,
            deleted,
        )

    except Exception:
        logger.exception("Usage reset scheduler monthly rollover failed")


async def start_usage_reset_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    if not settings.is_database_configured():
        logger.info("Usage reset scheduler: database not configured — skipping")
        return

    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        _monthly_rollover,
        trigger=CronTrigger(day=1, hour=0, minute=5),
        id="usage_monthly_rollover",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    _scheduler.start()
    logger.info("Usage reset scheduler started — monthly rollover fires on 1st of each month at 00:05 UTC")


def shutdown_usage_reset_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Usage reset scheduler stopped")
