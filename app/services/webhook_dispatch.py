"""Outbound webhook HTTP delivery and channel config parsing."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.crypto import decrypt_dsn
from app.infrastructure.database.models.notification_channel import NotificationChannel
from app.infrastructure.database.models.webhook_delivery import WebhookDelivery

logger = logging.getLogger(__name__)

_TEST_EVENT = "pulse.test"


def parse_channel_config(row: NotificationChannel) -> dict[str, Any]:
    if not row.config:
        return {}
    try:
        raw = decrypt_dsn(row.config)
        return json.loads(raw) if raw else {}
    except Exception:
        logger.warning("Could not decrypt channel config id=%s", row.id)
        return {}


def webhook_url_from_config(cfg: dict[str, Any]) -> str | None:
    url = cfg.get("url") or cfg.get("webhook_url")
    if isinstance(url, str) and url.strip().startswith(("http://", "https://")):
        return url.strip()
    return None


def is_deliverable_http_channel(row: NotificationChannel) -> bool:
    """True when the channel can receive JSON POSTs (webhook or Slack incoming webhook)."""
    if row.type not in ("webhook", "slack"):
        return False
    return webhook_url_from_config(parse_channel_config(row)) is not None


def channel_subscribes_to_event(cfg: dict[str, Any], event_type: str) -> bool:
    """If config.events is set, only deliver listed event types; otherwise deliver all."""
    raw = cfg.get("events")
    if not isinstance(raw, list) or len(raw) == 0:
        return True
    allowed = {str(x) for x in raw}
    return event_type in allowed


async def post_json_webhook(
    url: str,
    payload: dict[str, Any],
    *,
    timeout_s: float = 10.0,
) -> tuple[int | None, str | None]:
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(url, json=payload, headers={"Content-Type": "application/json"})
            body = (resp.text or "")[:4000]
            return resp.status_code, body
    except Exception as e:
        logger.warning("Webhook POST failed url=%s err=%s", url, e)
        return None, str(e)[:2000]


async def deliver_webhook_payload(
    db: AsyncSession,
    *,
    org_id: UUID,
    channel_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    alert_event_id: UUID | None = None,
) -> WebhookDelivery:
    """Create delivery row, POST to channel URL, persist outcome."""
    delivery = WebhookDelivery(
        org_id=org_id,
        channel_id=channel_id,
        alert_event_id=alert_event_id,
        event_type=event_type,
        payload=payload,
        status="pending",
        attempts=0,
    )
    db.add(delivery)
    await db.flush()
    await execute_pending_delivery(db, delivery)
    return delivery


async def execute_pending_delivery(db: AsyncSession, delivery: WebhookDelivery) -> None:
    ch = await db.get(NotificationChannel, delivery.channel_id)
    if not ch or ch.org_id != delivery.org_id:
        delivery.status = "failed"
        delivery.response_body = "Channel not found"
        delivery.last_attempt_at = datetime.now(timezone.utc)
        return

    cfg = parse_channel_config(ch)
    if is_deliverable_http_channel(ch) and not channel_subscribes_to_event(cfg, delivery.event_type):
        delivery.status = "skipped"
        delivery.response_body = "Event not subscribed on this channel"
        delivery.last_attempt_at = datetime.now(timezone.utc)
        return

    url = webhook_url_from_config(cfg)
    if not is_deliverable_http_channel(ch):
        delivery.status = "failed"
        delivery.response_body = "Channel is not a webhook/slack URL channel or url missing in config"
        delivery.last_attempt_at = datetime.now(timezone.utc)
        return

    delivery.attempts = int(delivery.attempts or 0) + 1
    delivery.last_attempt_at = datetime.now(timezone.utc)
    code, body = await post_json_webhook(url, delivery.payload)
    delivery.response_status = code
    delivery.response_body = (body or "")[:8000]
    if code is not None and 200 <= code < 300:
        delivery.status = "delivered"
        delivery.next_retry_at = None
    else:
        delivery.status = "failed"
        delivery.next_retry_at = None


async def send_channel_test(
    db: AsyncSession,
    *,
    org_id: UUID,
    channel_id: UUID,
) -> tuple[bool, str]:
    row = await db.get(NotificationChannel, channel_id)
    if not row or row.org_id != org_id:
        return False, "Channel not found"
    cfg = parse_channel_config(row)
    url = webhook_url_from_config(cfg)
    if not is_deliverable_http_channel(row):
        return False, (
            "Configure a webhook or Slack channel with config.url or config.webhook_url (https://...)"
        )
    payload = {
        "event": _TEST_EVENT,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    code, body = await post_json_webhook(url, payload)
    if code is not None and 200 <= code < 300:
        return True, f"Endpoint responded HTTP {code}"
    return False, f"HTTP {code}: {(body or '')[:500]}"
