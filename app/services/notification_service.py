"""In-app notification helpers (``notifications`` table)."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.infrastructure.database.models.org_notification import OrgNotification
from app.infrastructure.database.models.user import User

logger = logging.getLogger(__name__)

ADMIN_MANAGER_ROLES = ("admin", "manager")
HIGH_PRIORITY_URGENCIES = frozenset({"critical", "high"})


def dashboard_url(path: str) -> str:
    base = settings.FRONTEND_URL.rstrip("/")
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{base}{path}"


async def notify_users(
    db: AsyncSession,
    org_id: UUID,
    user_ids: list[UUID],
    *,
    title: str,
    body: str | None = None,
    type: str = "info",
    source: str | None = None,
    source_id: UUID | None = None,
    action_url: str | None = None,
) -> int:
    """Insert one in-app notification per user. Returns rows created."""
    if not user_ids:
        return 0
    seen: set[UUID] = set()
    created = 0
    for uid in user_ids:
        if uid in seen:
            continue
        seen.add(uid)
        db.add(
            OrgNotification(
                org_id=org_id,
                user_id=uid,
                title=title,
                body=body,
                type=type,
                source=source,
                source_id=source_id,
                action_url=action_url,
            )
        )
        created += 1
    return created


async def notify_roles(
    db: AsyncSession,
    org_id: UUID,
    roles: tuple[str, ...],
    *,
    title: str,
    body: str | None = None,
    type: str = "info",
    source: str | None = None,
    source_id: UUID | None = None,
    action_url: str | None = None,
) -> int:
    """Notify all active users in the org with one of the given roles."""
    result = await db.execute(
        select(User.id).where(
            User.org_id == org_id,
            User.is_active.is_(True),
            User.role.in_(roles),
        )
    )
    user_ids = [row[0] for row in result.all()]
    return await notify_users(
        db,
        org_id,
        user_ids,
        title=title,
        body=body,
        type=type,
        source=source,
        source_id=source_id,
        action_url=action_url,
    )


async def notify_admins_and_managers(
    db: AsyncSession,
    org_id: UUID,
    **kwargs,
) -> int:
    return await notify_roles(db, org_id, ADMIN_MANAGER_ROLES, **kwargs)


async def notify_high_priority_recommendations(
    db: AsyncSession,
    org_id: UUID,
    *,
    critical_count: int,
    high_count: int,
    pipeline_run_id: UUID | None,
) -> int:
    """Digest notification after a pipeline run creates critical/high recommendations."""
    total = critical_count + high_count
    if total <= 0:
        return 0
    parts: list[str] = []
    if critical_count:
        parts.append(f"{critical_count} critical")
    if high_count:
        parts.append(f"{high_count} high")
    summary = " and ".join(parts)
    body = (
        f"{summary} recommendation(s) are ready from the latest pipeline run. "
        "Review and action the highest-urgency items first."
    )
    return await notify_admins_and_managers(
        db,
        org_id,
        title="New high-priority recommendations",
        body=body,
        type="info",
        source="pipeline",
        source_id=pipeline_run_id,
        action_url=dashboard_url("/dashboard/recommendations"),
    )


async def notify_alert_triggered(
    db: AsyncSession,
    org_id: UUID,
    *,
    rule_name: str,
    metric: str,
    metric_value: float,
    operator: str,
    threshold: float,
    alert_event_id: UUID,
) -> int:
    body = (
        f"{metric} is {metric_value:g} (rule: {operator} {threshold:g}). "
        "Review affected entities and adjust your response playbook."
    )
    return await notify_admins_and_managers(
        db,
        org_id,
        title=f"Alert triggered: {rule_name}",
        body=body,
        type="warning",
        source="alert",
        source_id=alert_event_id,
        action_url=dashboard_url("/dashboard/alerts"),
    )


async def notify_pipeline_failed(
    db: AsyncSession,
    org_id: UUID,
    *,
    pipeline_run_id: UUID,
    error_message: str | None,
) -> int:
    body = (error_message or "The intelligence pipeline encountered an error.").strip()
    if len(body) > 500:
        body = body[:497] + "..."
    return await notify_admins_and_managers(
        db,
        org_id,
        title="Pipeline run failed",
        body=body,
        type="error",
        source="pipeline_run",
        source_id=pipeline_run_id,
        action_url=dashboard_url("/dashboard/pipeline"),
    )


async def notify_payment_failed(
    db: AsyncSession,
    org_id: UUID,
) -> int:
    """Pro subscription renewal failed — org admins (matches billing email audience)."""
    return await notify_roles(
        db,
        org_id,
        ("admin",),
        title="Pro payment failed",
        body=(
            "We could not renew your Pro subscription. Update your payment method "
            "in Plan & billing to avoid losing Pro features."
        ),
        type="error",
        source="billing",
        action_url=dashboard_url("/dashboard/plan"),
    )


async def notify_member_joined(
    db: AsyncSession,
    org_id: UUID,
    *,
    user_id: UUID,
    user_name: str | None,
    user_email: str,
    role: str,
) -> int:
    """Notify admins and managers when someone accepts an invitation (not the joiner)."""
    name = (user_name or "").strip() or user_email
    result = await db.execute(
        select(User.id).where(
            User.org_id == org_id,
            User.is_active.is_(True),
            User.role.in_(ADMIN_MANAGER_ROLES),
            User.id != user_id,
        )
    )
    recipient_ids = [row[0] for row in result.all()]
    return await notify_users(
        db,
        org_id,
        recipient_ids,
        title=f"{name} joined your team",
        body=f"{user_email} joined as {role}.",
        type="info",
        source="user",
        source_id=user_id,
        action_url=dashboard_url("/dashboard/team"),
    )


async def notify_connection_test_failed(
    db: AsyncSession,
    org_id: UUID,
    *,
    connection_id: UUID,
    connection_name: str,
    error_message: str | None,
) -> int:
    """Previously healthy connection failed a connectivity test."""
    name = (connection_name or "Data connection").strip()
    body = (error_message or "The connection test failed.").strip()
    if len(body) > 400:
        body = body[:397] + "..."
    return await notify_admins_and_managers(
        db,
        org_id,
        title=f"Connection failed: {name}",
        body=body,
        type="warning",
        source="connection",
        source_id=connection_id,
        action_url=dashboard_url("/dashboard/connections"),
    )
