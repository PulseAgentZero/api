"""Plan / license feature gating."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import plan_limit, plan_locked, rate_limited
from app.config.settings import settings
from app.infrastructure.database.models.organization import Organization
from app.services.self_hosted_license import resolve_self_hosted_entitlements

# ── Plan limits ───────────────────────────────────────────────────────────────
# None = unlimited. Self-hosted always resolves to pro-equivalent.
# Unknown plan strings fall back to free limits.

PLAN_LIMITS: dict[str, dict[str, int | None]] = {
    "free": {
        "api_keys": 1,
        "webhook_channels": 1,
        "users": 3,
        "connections": 5,
        "pipeline_runs_per_month": 20,
        "agent_queries_per_month": 100,
        "studio_dashboards": 5,
        "studio_query_executions_per_day": 600,
    },
    "pro": {
        "api_keys": None,
        "webhook_channels": None,
        "users": None,
        "connections": None,
        "pipeline_runs_per_month": None,
        "agent_queries_per_month": None,
        "studio_dashboards": None,
        "studio_query_executions_per_day": None,
    },
}


def get_plan_limits(plan: str) -> dict[str, int | None]:
    if settings.DEPLOYMENT_MODE == "self_hosted":
        return PLAN_LIMITS["pro"]
    return PLAN_LIMITS.get(plan.lower(), PLAN_LIMITS["free"])


# ── Cloud: feature -> minimum plans (audit_log still requires pro) ─────────────
# api_keys and webhook_deliveries are removed — replaced with count-based limits
# so free users can access them within their quota.
_CLOUD_FEATURE_PLAN: dict[str, tuple[str, ...]] = {
    "audit_log": ("pro",),
}


async def _get_org(db: AsyncSession, org_id: UUID) -> Organization | None:
    return await db.get(Organization, org_id)


async def require_cloud_plan(db: AsyncSession, org_id: UUID, min_plans: tuple[str, ...]) -> None:
    org = await _get_org(db, org_id)
    if org is None:
        return
    if settings.DEPLOYMENT_MODE == "self_hosted":
        return
    plan = (org.plan or "free").lower()
    if plan not in min_plans:
        raise plan_locked(
            "plan_upgrade",
            "This feature requires a higher plan.",
            upgrade_url=f"{settings.FRONTEND_URL.rstrip('/')}/pricing",
        )


async def require_feature(db: AsyncSession, org_id: UUID, feature: str) -> None:
    """Raise 402 FEATURE_LOCKED if org cannot use this feature."""
    needed = _CLOUD_FEATURE_PLAN.get(feature)
    if needed is None:
        return

    if settings.DEPLOYMENT_MODE == "self_hosted":
        ent = await resolve_self_hosted_entitlements(db, org_id)
        if ent.locked:
            raise plan_locked(
                feature,
                "License is invalid, expired, or must be revalidated with the license server.",
                upgrade_url=f"{settings.FRONTEND_URL.rstrip('/')}/settings/license",
            )
        if ent.plan in ("pro", "enterprise"):
            return
        if feature.lower() in ent.features:
            return
        raise plan_locked(
            feature,
            f"Feature '{feature}' is not included in your license.",
            upgrade_url=f"{settings.FRONTEND_URL.rstrip('/')}/settings/license",
        )

    org = await _get_org(db, org_id)
    if org is None:
        return
    plan = (org.plan or "free").lower()
    if plan not in needed:
        raise plan_locked(
            feature,
            f"Feature '{feature}' requires a Pro plan or higher.",
            upgrade_url=f"{settings.FRONTEND_URL.rstrip('/')}/pricing",
        )


async def max_cloud_free_connections(db: AsyncSession, org_id: UUID, active_count: int) -> None:
    if settings.DEPLOYMENT_MODE == "self_hosted":
        return
    org = await _get_org(db, org_id)
    if org is None:
        return
    plan = (org.plan or "free").lower()
    limit = get_plan_limits(plan).get("connections")
    if limit is not None and active_count >= limit:
        raise plan_limit(
            f"Active connection limit reached for your plan (max {limit}). "
            "Remove an existing connection or upgrade to Pro.",
        )


# ── Count-based limit helpers ─────────────────────────────────────────────────

async def check_api_key_limit(db: AsyncSession, org_id: UUID) -> None:
    """Raise PLAN_LIMIT if the org has reached its API key quota."""
    if settings.DEPLOYMENT_MODE == "self_hosted":
        return
    from app.infrastructure.database.models.api_key import ApiKey

    org = await _get_org(db, org_id)
    plan = (org.plan or "free").lower() if org else "free"
    limit = get_plan_limits(plan).get("api_keys")
    if limit is None:
        return
    count = int(
        await db.scalar(
            select(func.count()).select_from(ApiKey).where(
                ApiKey.org_id == org_id, ApiKey.revoked_at.is_(None)
            )
        )
        or 0
    )
    if count >= limit:
        raise plan_limit(
            f"API key limit reached for your plan (max {limit}). "
            "Revoke an existing key or upgrade to Pro.",
        )


async def check_studio_execution_budget(
    org_id: UUID, redis, plan: str = "free"
) -> None:
    """Raise RATE_LIMITED if the org has exceeded its daily studio query execution budget.

    Only enforced in cloud deployment. Pro plan and self-hosted always pass.
    Counter stored in Redis: incremented per execution, expires at end of day.
    """
    if settings.DEPLOYMENT_MODE != "cloud":
        return
    if redis is None:
        return
    limit = get_plan_limits(plan).get("studio_query_executions_per_day")
    if limit is None:
        return
    from app.infrastructure.redis.keys import studio_budget
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = studio_budget(str(org_id), today)
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, 86400)
    if count > limit:
        raise rate_limited(
            f"Studio execution budget exceeded ({limit} executions/day on the free plan). "
            "Upgrade to Pro for unlimited executions.",
        )


async def check_studio_dashboard_limit(db: AsyncSession, org_id: UUID) -> None:
    """Raise PLAN_LIMIT if the org has reached its studio dashboard quota.

    Free plan (cloud): 5 dashboards max.
    Pro plan (cloud) and all self-hosted: unlimited.
    """
    if settings.DEPLOYMENT_MODE == "self_hosted":
        return
    from app.infrastructure.database.models.studio_dashboard import StudioDashboard

    org = await _get_org(db, org_id)
    plan = (org.plan or "free").lower() if org else "free"
    limit = get_plan_limits(plan).get("studio_dashboards")
    if limit is None:
        return
    count = int(
        await db.scalar(
            select(func.count())
            .select_from(StudioDashboard)
            .where(StudioDashboard.org_id == org_id)
        )
        or 0
    )
    if count >= limit:
        raise plan_limit(
            f"Studio dashboard limit reached for the free plan (max {limit}). "
            "Upgrade to Pro for unlimited dashboards.",
        )


async def check_webhook_channel_limit(db: AsyncSession, org_id: UUID) -> None:
    """Raise PLAN_LIMIT if the org has reached its webhook channel quota."""
    if settings.DEPLOYMENT_MODE == "self_hosted":
        return
    from app.infrastructure.database.models.notification_channel import NotificationChannel

    org = await _get_org(db, org_id)
    plan = (org.plan or "free").lower() if org else "free"
    limit = get_plan_limits(plan).get("webhook_channels")
    if limit is None:
        return
    count = int(
        await db.scalar(
            select(func.count()).select_from(NotificationChannel).where(
                NotificationChannel.org_id == org_id,
                NotificationChannel.type == "webhook",
                NotificationChannel.is_active.is_(True),
            )
        )
        or 0
    )
    if count >= limit:
        raise plan_limit(
            f"Webhook channel limit reached for your plan (max {limit}). "
            "Delete an existing webhook channel or upgrade to Pro.",
        )


# ── Usage summary (for GET /organization/usage) ───────────────────────────────

async def get_usage_summary(db: AsyncSession, org_id: UUID) -> dict:
    """Return current usage counts against plan limits for the org."""
    from app.infrastructure.database.models.agent_conversation import AgentConversation
    from app.infrastructure.database.models.api_key import ApiKey
    from app.infrastructure.database.models.notification_channel import NotificationChannel
    from app.infrastructure.database.models.pipeline_run import PipelineRun
    from app.infrastructure.database.models.studio_dashboard import StudioDashboard
    from app.infrastructure.database.models.user import User
    from app.infrastructure.database.repositories.connection_repository import ConnectionRepository

    org = await _get_org(db, org_id)
    raw_plan = (org.plan or "free").lower() if org else "free"
    plan = "pro" if settings.DEPLOYMENT_MODE == "self_hosted" else raw_plan
    limits = get_plan_limits(plan)

    now = datetime.now(timezone.utc)
    first_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    connection_count = await ConnectionRepository(db).count_active(org_id)
    api_key_count = int(
        await db.scalar(
            select(func.count()).select_from(ApiKey).where(
                ApiKey.org_id == org_id, ApiKey.revoked_at.is_(None)
            )
        )
        or 0
    )
    webhook_channel_count = int(
        await db.scalar(
            select(func.count()).select_from(NotificationChannel).where(
                NotificationChannel.org_id == org_id,
                NotificationChannel.type == "webhook",
                NotificationChannel.is_active.is_(True),
            )
        )
        or 0
    )
    user_count = int(
        await db.scalar(
            select(func.count()).select_from(User).where(
                User.org_id == org_id, User.is_active.is_(True)
            )
        )
        or 0
    )
    pipeline_run_count = int(
        await db.scalar(
            select(func.count()).select_from(PipelineRun).where(
                PipelineRun.org_id == org_id,
                PipelineRun.created_at >= first_of_month,
            )
        )
        or 0
    )
    agent_query_count = int(
        await db.scalar(
            select(func.count()).select_from(AgentConversation).where(
                AgentConversation.org_id == org_id,
                AgentConversation.created_at >= first_of_month,
            )
        )
        or 0
    )
    studio_dashboard_count = int(
        await db.scalar(
            select(func.count()).select_from(StudioDashboard).where(
                StudioDashboard.org_id == org_id
            )
        )
        or 0
    )

    # Studio execution budget — read from Redis counter for today
    from app.infrastructure.redis.client import get_redis
    from app.infrastructure.redis.keys import studio_budget
    studio_exec_today = 0
    try:
        r = await get_redis()
        if r is not None:
            today = now.strftime("%Y-%m-%d")
            val = await r.get(studio_budget(str(org_id), today))
            studio_exec_today = int(val or 0)
    except Exception:
        pass

    def slot(used: int, key: str) -> dict:
        return {"used": used, "limit": limits.get(key)}

    return {
        "plan": plan,
        "limits": {
            "api_keys": slot(api_key_count, "api_keys"),
            "connections": slot(connection_count, "connections"),
            "webhook_channels": slot(webhook_channel_count, "webhook_channels"),
            "users": slot(user_count, "users"),
            "pipeline_runs_this_month": slot(pipeline_run_count, "pipeline_runs_per_month"),
            "agent_queries_this_month": slot(agent_query_count, "agent_queries_per_month"),
            "studio_dashboards": slot(studio_dashboard_count, "studio_dashboards"),
            "studio_executions_today": slot(studio_exec_today, "studio_query_executions_per_day"),
        },
    }
