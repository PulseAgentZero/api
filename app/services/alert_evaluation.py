"""Evaluate alert rules after a successful pipeline run and dispatch webhooks."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models.alert_event import AlertEvent
from app.infrastructure.database.models.alert_rule import AlertRule
from app.infrastructure.database.models.notification_channel import NotificationChannel
from app.infrastructure.database.models.entity_profile import EntityProfile
from app.infrastructure.database.models.recommendation import Recommendation
from app.services.webhook_dispatch import deliver_webhook_payload

logger = logging.getLogger(__name__)

_HIGH_TIERS = ("High", "Critical")
_MEDIUM_PLUS = ("Medium", "High", "Critical")


def _compare(op: str, value: Decimal, threshold: Decimal) -> bool:
    ops = {
        ">": value > threshold,
        ">=": value >= threshold,
        "<": value < threshold,
        "<=": value <= threshold,
        "==": value == threshold,
        "=": value == threshold,
        "!=": value != threshold,
        "eq": value == threshold,
        "ne": value != threshold,
        "gt": value > threshold,
        "gte": value >= threshold,
        "lt": value < threshold,
        "lte": value <= threshold,
    }
    return bool(ops.get(op.strip(), value >= threshold))


async def _metric_snapshot(
    db: AsyncSession,
    org_id: UUID,
    metric: str,
    entity_filter: dict[str, Any],
) -> tuple[Decimal, list[str], int] | None:
    """Return (metric value, sample entity ids, total affected count for the metric)."""
    base = [EntityProfile.org_id == org_id, EntityProfile.is_latest.is_(True)]
    if seg := entity_filter.get("segment"):
        if isinstance(seg, str) and seg:
            base.append(EntityProfile.segment == seg)

    m = (metric or "").strip().lower().replace(" ", "_")

    if m in ("high_risk_entities", "high_risk_entity_count", "count_high_risk"):
        cond = base + [EntityProfile.risk_tier.in_(_HIGH_TIERS)]
        cnt = int(await db.scalar(select(func.count()).select_from(EntityProfile).where(*cond)) or 0)
        q = (
            select(EntityProfile.entity_id)
            .where(*cond)
            .order_by(EntityProfile.risk_score.desc().nullslast())
            .limit(200)
        )
        ids = [r[0] for r in (await db.execute(q)).all()]
        return Decimal(cnt), ids, cnt

    if m in ("medium_plus_risk_entities",):
        cond = base + [EntityProfile.risk_tier.in_(_MEDIUM_PLUS)]
        cnt = int(await db.scalar(select(func.count()).select_from(EntityProfile).where(*cond)) or 0)
        q = select(EntityProfile.entity_id).where(*cond).limit(200)
        ids = [r[0] for r in (await db.execute(q)).all()]
        return Decimal(cnt), ids, cnt

    if m in ("avg_risk_score", "average_risk_score", "mean_risk_score"):
        avg = await db.scalar(select(func.avg(EntityProfile.risk_score)).where(*base))
        n = int(await db.scalar(select(func.count()).select_from(EntityProfile).where(*base)) or 0)
        return Decimal(str(avg or 0)), [], n

    if m in ("total_entities", "entity_count", "profiled_entities"):
        cnt = int(await db.scalar(select(func.count()).select_from(EntityProfile).where(*base)) or 0)
        return Decimal(cnt), [], cnt

    if m in ("open_recommendations", "open_recommendation_count"):
        cnt = (
            int(
                await db.scalar(
                    select(func.count())
                    .select_from(Recommendation)
                    .where(Recommendation.org_id == org_id, Recommendation.status == "open")
                )
                or 0
            )
        )
        return Decimal(cnt), [], cnt

    logger.debug("Unknown alert metric %r — skipping rule evaluation branch", metric)
    return None


async def evaluate_alerts_after_pipeline(
    db: AsyncSession,
    org_id: UUID,
    pipeline_run_id: UUID,
) -> None:
    """Create alert events and webhook deliveries when rules fire."""
    r = await db.execute(
        select(AlertRule).where(AlertRule.org_id == org_id, AlertRule.is_active.is_(True))
    )
    rules = list(r.scalars().all())
    now = datetime.now(timezone.utc)

    for rule in rules:
        try:
            snap = await _metric_snapshot(
                db, org_id, rule.metric, rule.entity_filter or {}
            )
            if snap is None:
                continue
            value, entity_ids, affected_total = snap
            threshold = Decimal(rule.threshold)
            if not _compare(rule.operator, value, threshold):
                continue

            if rule.last_triggered_at:
                delta = now - rule.last_triggered_at
                if delta < timedelta(minutes=max(1, rule.cooldown_minutes or 60)):
                    continue

            event = AlertEvent(
                org_id=org_id,
                rule_id=rule.id,
                pipeline_run_id=pipeline_run_id,
                metric=rule.metric,
                metric_value=value,
                threshold=threshold,
                affected_entity_ids=entity_ids[:500] or None,
                affected_count=affected_total,
            )
            db.add(event)
            await db.flush()

            rule.last_triggered_at = now

            payload = {
                "event": "pulse.alert.triggered",
                "org_id": str(org_id),
                "rule_id": str(rule.id),
                "rule_name": rule.name,
                "metric": rule.metric,
                "metric_value": float(value),
                "threshold": float(threshold),
                "operator": rule.operator,
                "alert_event_id": str(event.id),
                "pipeline_run_id": str(pipeline_run_id),
                "affected_sample": entity_ids[:50],
            }

            raw_ids = rule.channel_ids or []
            for cid in raw_ids:
                try:
                    ch_uuid = UUID(str(cid))
                except (ValueError, TypeError):
                    continue
                ch_row = await db.get(NotificationChannel, ch_uuid)
                if not ch_row or ch_row.org_id != org_id or ch_row.type != "webhook":
                    continue
                from app.services.webhook_dispatch import (
                    channel_subscribes_to_event,
                    parse_channel_config,
                )

                if not channel_subscribes_to_event(
                    parse_channel_config(ch_row), "alert.triggered"
                ):
                    continue
                try:
                    await deliver_webhook_payload(
                        db,
                        org_id=org_id,
                        channel_id=ch_uuid,
                        event_type="alert.triggered",
                        payload=payload,
                        alert_event_id=event.id,
                    )
                except Exception as e:
                    logger.warning("Webhook delivery for alert failed: %s", e)
        except Exception as e:
            logger.warning("Alert rule %s evaluation failed: %s", rule.id, e)
