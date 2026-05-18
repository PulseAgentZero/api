"""Alert rules, channels, and events (BACKEND_ROUTES §12)."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.role_deps import require_role
from app.api.dependencies.plan_gate import check_webhook_channel_limit
from app.api.errors import not_found
from app.infrastructure.crypto import encrypt_dsn
import json
from app.infrastructure.database.models.alert_event import AlertEvent
from app.infrastructure.database.models.alert_rule import AlertRule
from app.infrastructure.database.models.notification_channel import NotificationChannel
from app.infrastructure.database.models.user import User
from app.infrastructure.database.session import get_db

router = APIRouter(prefix="/alerts", tags=["Alerts"])


def _rule_out(r: AlertRule) -> dict:
    return {
        "id": str(r.id),
        "name": r.name,
        "description": r.description,
        "metric": r.metric,
        "operator": r.operator,
        "threshold": float(r.threshold),
        "entity_filter": r.entity_filter,
        "is_active": r.is_active,
        "cooldown_minutes": r.cooldown_minutes,
        "last_triggered_at": r.last_triggered_at.isoformat() if r.last_triggered_at else None,
        "channel_ids": list(r.channel_ids or []),
        "created_at": r.created_at.isoformat(),
    }


@router.get("/rules")
async def list_rules(
    current_user: User = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List all alert rules configured for the org. Requires admin or manager role."""
    result = await db.execute(select(AlertRule).where(AlertRule.org_id == current_user.org_id))
    rows = list(result.scalars().all())
    return {"rules": [_rule_out(r) for r in rows]}


class RuleBody(BaseModel):
    name: str
    description: str | None = None
    metric: str
    operator: str
    threshold: Decimal
    entity_filter: dict = Field(default_factory=dict)
    channel_ids: list[UUID] = Field(default_factory=list)
    cooldown_minutes: int = 60


async def _validate_channels(db: AsyncSession, org_id: UUID, ids: list[UUID]) -> None:
    for cid in ids:
        ch = await db.get(NotificationChannel, cid)
        if not ch or ch.org_id != org_id:
            raise not_found("Channel not found")


@router.post("/rules", status_code=status.HTTP_201_CREATED)
async def create_rule(
    body: RuleBody,
    current_user: User = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create an alert rule. Requires admin or manager role.

    **metric** — the field to evaluate, e.g. `"risk_score"`.
    **operator** — `"gt"`, `"lt"`, `"gte"`, `"lte"`, `"eq"`.
    **threshold** — numeric value that triggers the alert.
    **channel_ids** — list of notification channel UUIDs to notify on trigger.
    **cooldown_minutes** — minimum minutes between consecutive alerts for the same rule (default 60).
    """
    await _validate_channels(db, current_user.org_id, body.channel_ids)
    row = AlertRule(
        org_id=current_user.org_id,
        created_by=current_user.id,
        name=body.name,
        description=body.description,
        metric=body.metric,
        operator=body.operator,
        threshold=body.threshold,
        entity_filter=body.entity_filter,
        channel_ids=[str(x) for x in body.channel_ids],
        cooldown_minutes=body.cooldown_minutes,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _rule_out(row)


@router.put("/rules/{rule_id}")
async def update_rule(
    rule_id: UUID,
    body: RuleBody,
    current_user: User = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Replace all fields of an alert rule. Requires admin or manager role."""
    row = await db.get(AlertRule, rule_id)
    if not row or row.org_id != current_user.org_id:
        raise not_found()
    await _validate_channels(db, current_user.org_id, body.channel_ids)
    row.name = body.name
    row.description = body.description
    row.metric = body.metric
    row.operator = body.operator
    row.threshold = body.threshold
    row.entity_filter = body.entity_filter
    row.channel_ids = [str(x) for x in body.channel_ids]
    row.cooldown_minutes = body.cooldown_minutes
    await db.commit()
    await db.refresh(row)
    return _rule_out(row)


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(
    rule_id: UUID,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Delete an alert rule permanently. Requires admin role."""
    row = await db.get(AlertRule, rule_id)
    if not row or row.org_id != current_user.org_id:
        raise not_found()
    await db.delete(row)
    await db.commit()


def _channel_out(c: NotificationChannel) -> dict:
    from app.services.webhook_dispatch import parse_channel_config, webhook_url_from_config

    out: dict = {
        "id": str(c.id),
        "name": c.name,
        "type": c.type,
        "is_active": c.is_active,
        "created_at": c.created_at.isoformat(),
    }
    if c.type == "webhook":
        cfg = parse_channel_config(c)
        events = cfg.get("events")
        if isinstance(events, list):
            out["events"] = [str(x) for x in events]
        url = webhook_url_from_config(cfg)
        if url:
            out["url_hint"] = url if len(url) <= 56 else f"{url[:53]}…"
    return out


@router.get("/channels")
async def list_channels(
    current_user: User = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List all notification channels for the org. Requires admin or manager role."""
    result = await db.execute(
        select(NotificationChannel).where(NotificationChannel.org_id == current_user.org_id)
    )
    rows = list(result.scalars().all())
    return {"channels": [_channel_out(c) for c in rows]}


class ChannelBody(BaseModel):
    name: str
    type: str
    config: dict = Field(default_factory=dict)


@router.post("/channels", status_code=status.HTTP_201_CREATED)
async def create_channel(
    body: ChannelBody,
    current_user: User = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create a notification channel. Requires admin or manager role.

    **type** — `"slack"`, `"webhook"`, `"email"`.
    **config** — channel-specific credentials:
    - Slack: `{ "webhook_url": "https://hooks.slack.com/..." }`
    - Webhook: `{ "url": "https://...", "headers": {} }`
    - Email: `{ "to": "ops@company.com" }`
    """
    if body.type == "webhook":
        await check_webhook_channel_limit(db, current_user.org_id)
    enc = encrypt_dsn(json.dumps(body.config)) if body.config else None
    row = NotificationChannel(
        org_id=current_user.org_id,
        name=body.name,
        type=body.type,
        config=enc,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {
        "id": str(row.id),
        "name": row.name,
        "type": row.type,
        "is_active": row.is_active,
        "created_at": row.created_at.isoformat(),
    }


@router.put("/channels/{channel_id}")
async def update_channel(
    channel_id: UUID,
    body: ChannelBody,
    current_user: User = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Replace all fields of a notification channel. Requires admin or manager role."""
    row = await db.get(NotificationChannel, channel_id)
    if not row or row.org_id != current_user.org_id:
        raise not_found()
    row.name = body.name
    row.type = body.type
    row.config = encrypt_dsn(json.dumps(body.config)) if body.config else None
    await db.commit()
    await db.refresh(row)
    return {
        "id": str(row.id),
        "name": row.name,
        "type": row.type,
        "is_active": row.is_active,
        "created_at": row.created_at.isoformat(),
    }


@router.delete("/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(
    channel_id: UUID,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Delete a notification channel permanently. Requires admin role."""
    row = await db.get(NotificationChannel, channel_id)
    if not row or row.org_id != current_user.org_id:
        raise not_found()
    await db.delete(row)
    await db.commit()


@router.post("/channels/{channel_id}/test")
async def test_channel(
    channel_id: UUID,
    current_user: User = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Send a test payload to a notification channel to verify it is configured correctly.

    Returns `{ "success": true, "message": "..." }` on success, or 422 on failure.
    """
    from app.api.errors import bad_request, not_found
    from app.services.webhook_dispatch import send_channel_test

    ok, msg = await send_channel_test(db, org_id=current_user.org_id, channel_id=channel_id)
    if not ok:
        if "not found" in msg.lower():
            raise not_found()
        raise bad_request("WEBHOOK_TEST_FAILED", msg)
    return {"success": True, "message": msg}


@router.get("/events")
async def list_events(
    rule_id: UUID | None = None,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List alert events (triggered rule firings), most recent first.

    Filter by `rule_id` to see events for a specific rule. Paginated, default 50 per page.
    Each event records the metric value, threshold, and how many entities were affected.
    """
    stmt = select(AlertEvent).where(AlertEvent.org_id == current_user.org_id)
    if rule_id:
        stmt = stmt.where(AlertEvent.rule_id == rule_id)
    stmt = stmt.order_by(AlertEvent.created_at.desc()).offset((page - 1) * limit).limit(limit)
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    out = []
    for e in rows:
        rule = await db.get(AlertRule, e.rule_id)
        out.append(
            {
                "id": str(e.id),
                "rule_id": str(e.rule_id),
                "rule_name": rule.name if rule else None,
                "metric": e.metric,
                "metric_value": float(e.metric_value),
                "threshold": float(e.threshold),
                "affected_count": e.affected_count,
                "resolved_at": e.resolved_at.isoformat() if e.resolved_at else None,
                "created_at": e.created_at.isoformat(),
            }
        )
    return {"events": out}
