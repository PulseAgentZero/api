"""Cloud subscription entitlements — grace period and effective plan resolution."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.infrastructure.database.models.organization import Organization
from app.infrastructure.database.models.subscription import Subscription

_PAID_PLANS = frozenset({"growth", "pro", "enterprise"})


def resolve_org_plan(raw_plan: str | None) -> str:
    """Normalize stored org/subscription plan string."""
    if settings.DEPLOYMENT_MODE == "self_hosted":
        return "pro"
    return (raw_plan or "free").lower()


def grace_ends_at(sub: Subscription | None) -> datetime | None:
    if sub is None or sub.status != "attention" or sub.payment_failed_at is None:
        return None
    failed_at = sub.payment_failed_at
    if failed_at.tzinfo is None:
        failed_at = failed_at.replace(tzinfo=timezone.utc)
    return failed_at + timedelta(days=settings.BILLING_GRACE_DAYS)


def is_grace_expired(sub: Subscription | None, *, now: datetime | None = None) -> bool:
    end = grace_ends_at(sub)
    if end is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now >= end


async def get_subscription(db: AsyncSession, org_id: UUID) -> Subscription | None:
    result = await db.execute(select(Subscription).where(Subscription.org_id == org_id))
    return result.scalar_one_or_none()


async def get_effective_cloud_plan(
    db: AsyncSession,
    org_id: UUID,
    *,
    org: Organization | None = None,
    sub: Subscription | None = None,
) -> str:
    """Plan used for limits and feature gates (accounts for billing grace expiry)."""
    if settings.DEPLOYMENT_MODE == "self_hosted":
        return "pro"
    if org is None:
        org = await db.get(Organization, org_id)
    if sub is None:
        sub = await get_subscription(db, org_id)
    raw = resolve_org_plan(org.plan if org else None)
    if sub and sub.status == "attention" and is_grace_expired(sub):
        return "free"
    return raw


def paystack_plan_code_for_tier(tier: str) -> str | None:
    """Map Pulse plan tier to Paystack plan code env var."""
    tier = tier.lower()
    if tier == "pro":
        return settings.PAYSTACK_PRO_PLAN_CODE
    if tier == "growth":
        return settings.PAYSTACK_GROWTH_PLAN_CODE
    return None


def tier_from_paystack_plan_code(plan_code: str | None) -> str:
    if plan_code and settings.PAYSTACK_GROWTH_PLAN_CODE and plan_code == settings.PAYSTACK_GROWTH_PLAN_CODE:
        return "growth"
    return "pro"


def subscription_response(sub: Subscription, *, effective_plan: str | None = None) -> dict:
    """Serialize subscription row for API responses."""
    grace_end = grace_ends_at(sub)
    now = datetime.now(timezone.utc)
    in_grace = (
        sub.status == "attention"
        and grace_end is not None
        and now < grace_end
    )
    return {
        "plan": sub.plan,
        "effective_plan": effective_plan if effective_plan is not None else sub.plan,
        "status": sub.status,
        "paystack_subscription_code": sub.paystack_subscription_code,
        "next_payment_date": sub.next_payment_date,
        "payment_failed_at": sub.payment_failed_at,
        "grace_ends_at": grace_end,
        "payment_attention": in_grace,
        "manage_link_available": bool(sub.paystack_subscription_code),
        "updated_at": sub.updated_at,
    }


async def downgrade_org_after_grace(db: AsyncSession, sub: Subscription) -> bool:
    """Set org to free when grace has expired. Returns True if downgraded."""
    if not is_grace_expired(sub):
        return False
    org = await db.get(Organization, sub.org_id)
    if org and (org.plan or "free").lower() in _PAID_PLANS:
        org.plan = "free"
    sub.plan = "free"
    return True
