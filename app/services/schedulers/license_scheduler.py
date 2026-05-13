"""Periodic license revalidation against the license server (self-hosted only)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.config.settings import settings
from app.infrastructure.database.models.organization import Organization
from app.infrastructure.database.models.license_key import LicenseKey
from app.infrastructure.database.session import async_session_factory
from app.services.license_remote import post_validate_license

logger = logging.getLogger(__name__)

_license_scheduler: AsyncIOScheduler | None = None


def _apply_success_to_row(row: LicenseKey, data: dict, now: datetime) -> None:
    if data.get("plan"):
        row.plan = str(data["plan"])
    if "features" in data and data["features"] is not None:
        row.features = list(data["features"])
    if "seat_limit" in data:
        row.seat_limit = data.get("seat_limit")
    expires_at = data.get("expires_at")
    if expires_at:
        try:
            row.expires_at = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except ValueError:
            pass
    row.last_validated_at = now
    row.validation_cached_until = now + timedelta(days=settings.LICENSE_OFFLINE_GRACE_DAYS)
    row.is_active = bool(data.get("valid", True))


async def _revalidate_all_licenses() -> None:
    if settings.DEPLOYMENT_MODE != "self_hosted":
        return
    now = datetime.now(timezone.utc)
    try:
        async with async_session_factory() as session:
            r = await session.execute(select(LicenseKey).where(LicenseKey.is_active.is_(True)))
            rows = list(r.scalars().all())
            for row in rows:
                org = await session.get(Organization, row.org_id)
                code, data, _err = await post_validate_license(row.license_key, row.org_id)
                if code == 0:
                    logger.info("Skipping license revalidation for org %s (server unreachable)", row.org_id)
                    continue
                if code < 300 and data and data.get("valid", True) is not False:
                    _apply_success_to_row(row, data, now)
                    if org is not None:
                        org.plan = str(row.plan or "free")
                else:
                    row.is_active = False
                    if org is not None:
                        org.plan = "free"
                    logger.warning("License deactivated for org %s (server returned invalid)", row.org_id)
            await session.commit()
    except Exception:
        logger.exception("License revalidation cycle failed")


async def start_license_scheduler() -> AsyncIOScheduler | None:
    global _license_scheduler
    if settings.DEPLOYMENT_MODE != "self_hosted":
        return None
    if not settings.is_database_configured():
        return None

    hours = max(1, settings.LICENSE_REVALIDATION_INTERVAL_HOURS)
    _license_scheduler = AsyncIOScheduler()
    _license_scheduler.add_job(
        _revalidate_all_licenses,
        trigger=IntervalTrigger(hours=hours),
        id="pulse_license_revalidation",
        replace_existing=True,
    )
    _license_scheduler.start()
    logger.info("License revalidation scheduler started (every %sh)", hours)
    return _license_scheduler


def shutdown_license_scheduler() -> None:
    global _license_scheduler
    if _license_scheduler is not None:
        _license_scheduler.shutdown(wait=False)
        _license_scheduler = None
        logger.info("License revalidation scheduler shut down")
