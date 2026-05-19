"""Daily billing scheduler — renewal reminders and grace-period enforcement."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.config.settings import settings
from app.infrastructure.database.models.subscription import Subscription
from app.infrastructure.database.models.user import User
from app.infrastructure.database.models.organization import Organization
from app.infrastructure.database.session import async_session_factory
from app.services.email_queue import queue_email
from app.services.billing_entitlements import downgrade_org_after_grace, is_grace_expired

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def _send_renewal_reminders() -> None:
    """Query subscriptions renewing within 24 hours and email each org admin."""
    if settings.DEPLOYMENT_MODE == "self_hosted":
        return

    now = datetime.now(timezone.utc)
    window_start = now
    window_end = now + timedelta(hours=24)

    async with async_session_factory() as session:
        result = await session.execute(
            select(Subscription).where(
                Subscription.status.in_(["active", "non-renewing"]),
                Subscription.next_payment_date >= window_start,
                Subscription.next_payment_date <= window_end,
            )
        )
        subscriptions = result.scalars().all()

        if not subscriptions:
            logger.debug("Billing scheduler: no renewals in the next 24 hours")
            return

        logger.info("Billing scheduler: sending renewal reminders for %d subscription(s)", len(subscriptions))

        for sub in subscriptions:
            try:
                admin_result = await session.execute(
                    select(User.email)
                    .where(
                        User.org_id == sub.org_id,
                        User.role == "admin",
                        User.is_active.is_(True),
                    )
                    .limit(1)
                )
                admin_row = admin_result.first()
                if not admin_row:
                    continue

                org = await session.get(Organization, sub.org_id)
                org_name = org.name if org else "your organisation"

                renewal_date = (
                    sub.next_payment_date.strftime("%B %d, %Y")
                    if sub.next_payment_date
                    else "tomorrow"
                )

                await queue_email(
                    "subscription_renewal_reminder",
                    to=admin_row[0],
                    org_name=org_name,
                    renewal_date=renewal_date,
                )
                logger.info("Renewal reminder sent to %s (org %s)", admin_row[0], sub.org_id)

            except Exception:
                logger.exception("Failed to send renewal reminder for subscription %s", sub.id)


async def _enforce_grace_period_expiry() -> None:
    """Downgrade orgs whose payment-failure grace period has ended."""
    if settings.DEPLOYMENT_MODE == "self_hosted":
        return

    now = datetime.now(timezone.utc)
    async with async_session_factory() as session:
        result = await session.execute(
            select(Subscription).where(
                Subscription.status == "attention",
                Subscription.payment_failed_at.isnot(None),
            )
        )
        subs = result.scalars().all()
        downgraded = 0
        for sub in subs:
            if not is_grace_expired(sub, now=now):
                continue
            if await downgrade_org_after_grace(session, sub):
                downgraded += 1
                logger.info(
                    "Billing scheduler: grace expired for org %s — downgraded to free",
                    sub.org_id,
                )
        if downgraded:
            await session.commit()


async def start_billing_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return

    if settings.DEPLOYMENT_MODE == "self_hosted":
        logger.info("Billing scheduler: skipped (self-hosted deployment)")
        return

    _scheduler = AsyncIOScheduler(timezone="UTC")
    if settings.is_email_configured():
        _scheduler.add_job(
            _send_renewal_reminders,
            trigger=CronTrigger(hour=9, minute=0),
            id="billing_renewal_reminders",
            replace_existing=True,
            misfire_grace_time=3600,
        )
    else:
        logger.info("Billing scheduler: email not configured — renewal reminders skipped")

    _scheduler.add_job(
        _enforce_grace_period_expiry,
        trigger=CronTrigger(hour=10, minute=0),
        id="billing_grace_expiry",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    _scheduler.start()
    logger.info("Billing scheduler started (renewal reminders 09:00 UTC, grace enforcement 10:00 UTC)")


def shutdown_billing_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Billing scheduler stopped")
