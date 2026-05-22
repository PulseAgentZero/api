"""Periodic LDAP sync for orgs with active ldap_configurations."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from croniter import croniter
from sqlalchemy import select

from app.config.settings import settings
from app.infrastructure.database.models.ldap_configuration import LdapConfiguration
from app.infrastructure.database.session import async_session_factory
from app.services.ldap_sync import sync_org_ldap

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def _ensure_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _tick() -> None:
    if settings.DEPLOYMENT_MODE != "self_hosted":
        return
    now = datetime.now(timezone.utc)
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(LdapConfiguration).where(LdapConfiguration.is_active.is_(True))
            )
        ).scalars().all()
        for cfg in rows:
            try:
                cron = croniter(cfg.sync_schedule_cron, now)
                prev_run = _ensure_aware(cron.get_prev(datetime))
                last = _ensure_aware(cfg.last_sync_at)
                if last and prev_run and last >= prev_run:
                    continue
                await sync_org_ldap(db, cfg.org_id)
            except Exception:
                logger.exception("LDAP sync failed for org %s", cfg.org_id)
        await db.commit()


def start_ldap_sync_scheduler() -> None:
    global _scheduler
    if settings.DEPLOYMENT_MODE != "self_hosted":
        return
    if _scheduler is not None:
        return
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        _tick,
        trigger=IntervalTrigger(minutes=15),
        id="ldap_sync",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    _scheduler.start()
    logger.info("LDAP sync scheduler started")


def shutdown_ldap_sync_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
