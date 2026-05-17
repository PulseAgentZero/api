"""Daily billing scheduler — sends subscription renewal reminders.

Runs once per day at 09:00 UTC. Finds every active cloud subscription whose
next_payment_date falls within the next 24 hours and emails the org admin a
heads-up so they can check their payment method is still valid.
"""

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
from app.infrastructure.email.sender import send_subscription_renewal_reminder_email

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def _send_renewal_reminders() -> None:
    """Query subscriptions renewing within 24 hours and email each org admin."""
    if settings.DEPLOYMENT_MODE == "self_hosted":
        return  # subscriptions are cloud-only

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
                # Get org admin email
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

                # Get org name
                org = await session.get(Organization, sub.org_id)
                org_name = org.name if org else "your organisation"

                renewal_date = (
                    sub.next_payment_date.strftime("%B %d, %Y")
                    if sub.next_payment_date
                    else "tomorrow"
                )

                await send_subscription_renewal_reminder_email(
                    to=admin_row[0],
                    org_name=org_name,
                    renewal_date=renewal_date,
                )
                logger.info("Renewal reminder sent to %s (org %s)", admin_row[0], sub.org_id)

            except Exception:
                logger.exception("Failed to send renewal reminder for subscription %s", sub.id)


async def start_billing_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return

    if not settings.is_email_configured():
        logger.info("Billing scheduler: email not configured — renewal reminders skipped")
        return

    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        _send_renewal_reminders,
        trigger=CronTrigger(hour=9, minute=0),
        id="billing_renewal_reminders",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    _scheduler.start()
    logger.info("Billing scheduler started — renewal reminders fire daily at 09:00 UTC")


def shutdown_billing_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Billing scheduler stopped")
