"""Redis-backed retry queue when self-hosted license issuance fails after payment."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.infrastructure.redis.client import get_redis

logger = logging.getLogger(__name__)

REDIS_ZSET = "selfhost_pending_issuance"
# 5m, 15m, 1h, 6h, 24h, 24h (then stop after MAX_ATTEMPTS)
BACKOFF_SEC = (300, 900, 3600, 21600, 86400, 86400)
MAX_ATTEMPTS = len(BACKOFF_SEC)
TTL_SEC = 7 * 24 * 3600


def _member_key(payment_reference: str) -> str:
    return payment_reference.strip()


async def enqueue_pending_issuance(
    *,
    payment_reference: str,
    delivery_email: str,
    purchaser_org_id: str | None,
    attempt: int = 0,
) -> None:
    """Schedule a retry for license issuance (no-op if Redis unavailable)."""
    r = await get_redis()
    if r is None:
        logger.warning(
            "Redis unavailable; cannot queue pending license issuance for %s",
            payment_reference,
        )
        return

    ref = _member_key(payment_reference)
    if attempt >= MAX_ATTEMPTS:
        logger.error(
            "Self-hosted license issuance exhausted retries for payment %s", ref
        )
        return

    delay = BACKOFF_SEC[attempt]
    run_at = time.time() + delay
    payload: dict[str, Any] = {
        "payment_reference": ref,
        "delivery_email": delivery_email.strip().lower(),
        "purchaser_org_id": purchaser_org_id,
        "attempt": attempt,
    }
    try:
        await r.zadd(REDIS_ZSET, {json.dumps(payload, separators=(",", ":")): run_at})
        await r.expire(REDIS_ZSET, TTL_SEC)
        logger.info(
            "Queued pending license issuance for %s (attempt %s, in %ss)",
            ref,
            attempt + 1,
            delay,
        )
    except Exception:
        logger.warning(
            "Failed to enqueue pending license issuance for %s",
            ref,
            exc_info=True,
        )


async def remove_pending_issuance(payment_reference: str) -> None:
    """Drop all queued retries for a payment reference after successful issuance."""
    r = await get_redis()
    if r is None:
        return
    ref = _member_key(payment_reference)
    try:
        members = await r.zrange(REDIS_ZSET, 0, -1)
        for raw in members or []:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if data.get("payment_reference") == ref:
                await r.zrem(REDIS_ZSET, raw)
    except Exception:
        logger.warning(
            "Failed to remove pending license issuance for %s", ref, exc_info=True
        )


async def process_due_pending_issuances() -> int:
    """Retry license issuance for all payments whose backoff window has elapsed."""
    from app.api.routes.billing import _issue_and_deliver_self_hosted_license

    r = await get_redis()
    if r is None:
        return 0

    now = time.time()
    processed = 0
    try:
        due = await r.zrangebyscore(REDIS_ZSET, "-inf", now)
    except Exception:
        logger.exception("Failed to read pending license issuance queue")
        return 0

    for raw in due or []:
        member = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        try:
            data = json.loads(member)
        except json.JSONDecodeError:
            await r.zrem(REDIS_ZSET, member)
            continue

        ref = str(data.get("payment_reference") or "").strip()
        email = str(data.get("delivery_email") or "").strip().lower()
        org_id = data.get("purchaser_org_id")
        attempt = int(data.get("attempt") or 0)
        if not ref or not email:
            await r.zrem(REDIS_ZSET, member)
            continue

        await r.zrem(REDIS_ZSET, member)

        license_key, _expires = await _issue_and_deliver_self_hosted_license(
            payment_reference=ref,
            delivery_email=email,
            purchaser_org_id=str(org_id) if org_id else None,
        )
        if license_key:
            processed += 1
            logger.info("Pending license issuance succeeded for payment %s", ref)
            continue

        next_attempt = attempt + 1
        if next_attempt < MAX_ATTEMPTS:
            await enqueue_pending_issuance(
                payment_reference=ref,
                delivery_email=email,
                purchaser_org_id=str(org_id) if org_id else None,
                attempt=next_attempt,
            )
        else:
            logger.error(
                "Pending license issuance failed after %s attempts for payment %s",
                MAX_ATTEMPTS,
                ref,
            )

    return processed
