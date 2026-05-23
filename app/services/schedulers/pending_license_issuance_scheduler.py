"""Retry self-hosted license issuance when the license server was unreachable at payment time."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config.settings import settings
from app.services.selfhost_license_pending import process_due_pending_issuances

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def _run_pending_license_issuance() -> None:
    if settings.DEPLOYMENT_MODE == "self_hosted":
        return
    if not settings.is_database_configured():
        return
    count = await process_due_pending_issuances()
    if count:
        logger.info("Pending license issuance job delivered %s key(s)", count)


async def start_pending_license_issuance_scheduler() -> AsyncIOScheduler | None:
    global _scheduler
    if settings.DEPLOYMENT_MODE == "self_hosted":
        return None
    if not settings.is_database_configured():
        return None

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        _run_pending_license_issuance,
        trigger=IntervalTrigger(minutes=5),
        id="selfhost_pending_license_issuance",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("Pending self-hosted license issuance scheduler started (every 5m)")
    return _scheduler


def shutdown_pending_license_issuance_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Pending self-hosted license issuance scheduler shut down")
