"""Periodic prune of low-importance / aged-out conversational memory."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.config.settings import settings
from app.infrastructure.database.models.organization import Organization
from app.infrastructure.database.session import async_session_factory
from app.services.conversational_memory import prune as prune_conv_memory

logger = logging.getLogger(__name__)

_memory_scheduler: AsyncIOScheduler | None = None


async def _prune_all_orgs() -> None:
    if not settings.CONV_MEMORY_ENABLED:
        return
    try:
        async with async_session_factory() as session:
            org_ids = (await session.execute(select(Organization.id))).scalars().all()
        total_removed = 0
        for org_id in org_ids:
            removed = await prune_conv_memory(org_id)
            total_removed += int(removed or 0)
        if total_removed:
            logger.info(
                "[conv_memory] prune cycle removed %d points across %d orgs",
                total_removed,
                len(org_ids),
            )
    except Exception:
        logger.exception("Conversational memory prune cycle failed")


async def start_memory_prune_scheduler() -> AsyncIOScheduler | None:
    """Daily prune of conversational memory across every org."""
    global _memory_scheduler
    if not settings.CONV_MEMORY_ENABLED or not settings.is_database_configured():
        return None

    _memory_scheduler = AsyncIOScheduler()
    _memory_scheduler.add_job(
        _prune_all_orgs,
        trigger=IntervalTrigger(hours=24),
        id="pulse_conv_memory_prune",
        replace_existing=True,
    )
    _memory_scheduler.start()
    logger.info("Conversational memory prune scheduler started (every 24h)")
    return _memory_scheduler


def shutdown_memory_prune_scheduler() -> None:
    global _memory_scheduler
    if _memory_scheduler is not None:
        _memory_scheduler.shutdown(wait=False)
        _memory_scheduler = None
        logger.info("Conversational memory prune scheduler shut down")
