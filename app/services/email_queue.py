"""Redis queue for transactional email (worker consumer; in-process fallback)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.infrastructure.redis.client import get_redis

logger = logging.getLogger(__name__)

EMAIL_QUEUE_KEY = "pulse:email:queue"


async def dispatch_email_job(data: dict[str, Any]) -> None:
    """Render and send one email (used by worker and in-process fallback)."""
    from app.infrastructure.email.sender import (
        send_invitation_email,
        send_license_key_email,
        send_password_reset_email,
        send_subscription_failed_email,
        send_subscription_renewal_reminder_email,
        send_subscription_success_email,
        send_verification_email,
        send_welcome_email,
    )

    email_type = data.get("email_type")
    to = data.get("to", "")
    try:
        if email_type == "verification":
            await send_verification_email(to, data["token"])
        elif email_type == "password_reset":
            await send_password_reset_email(to, data["token"])
        elif email_type == "welcome":
            await send_welcome_email(
                to,
                full_name=data.get("full_name", ""),
                org_name=data["org_name"],
            )
        elif email_type == "invitation":
            await send_invitation_email(
                to,
                data["token"],
                data["invited_by"],
                data["org_name"],
                role=data.get("role", "member"),
            )
        elif email_type == "subscription_success":
            await send_subscription_success_email(to, data["org_name"], data["next_payment_date"])
        elif email_type == "subscription_failed":
            await send_subscription_failed_email(to, data["org_name"])
        elif email_type == "subscription_renewal_reminder":
            await send_subscription_renewal_reminder_email(
                to, data["org_name"], data["renewal_date"]
            )
        elif email_type == "license_key":
            await send_license_key_email(
                to, data["license_key"], data.get("expires_at")
            )
        else:
            logger.warning("Unknown email job type: %s", email_type)
    except Exception:
        logger.exception("Email job failed type=%s to=%s", email_type, to)


async def enqueue_email_job(email_type: str, **fields: Any) -> bool:
    """Push email job to Redis. Returns False if Redis is unavailable."""
    r = await get_redis()
    if r is None:
        return False
    payload = json.dumps({"job_type": "email", "email_type": email_type, **fields})
    await r.rpush(EMAIL_QUEUE_KEY, payload)
    logger.debug("Enqueued email job type=%s to=%s", email_type, fields.get("to"))
    return True


async def queue_email(email_type: str, **fields: Any) -> None:
    """Enqueue for worker delivery, or run in-process if Redis is unavailable."""
    if await enqueue_email_job(email_type, **fields):
        return

    async def _run() -> None:
        try:
            await dispatch_email_job({"email_type": email_type, **fields})
        except Exception:
            logger.exception(
                "In-process email failed type=%s to=%s", email_type, fields.get("to")
            )

    asyncio.create_task(_run())
