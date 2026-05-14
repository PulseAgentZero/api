"""Plan / license feature gating (BACKEND_ROUTES + SCHEMA + LICENSE_SYSTEM.md)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import plan_limit, plan_locked
from app.config.settings import settings
from app.infrastructure.database.models.organization import Organization
from app.services.self_hosted_license import resolve_self_hosted_entitlements


async def _get_org(db: AsyncSession, org_id: UUID) -> Organization | None:
    return await db.get(Organization, org_id)


# Cloud: feature -> minimum plan
_CLOUD_FEATURE_PLAN: dict[str, tuple[str, ...]] = {
    "advanced_analytics": ("pro", "enterprise"),
    "api_keys": ("pro", "enterprise"),
    "webhook_deliveries": ("pro", "enterprise"),
    "audit_log": ("pro", "enterprise"),
}


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
        feat_l = feature.lower()
        if ent.plan in ("pro", "enterprise"):
            return
        if feat_l in ent.features:
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
    if (org.plan or "free").lower() == "free" and active_count >= 1:
        raise plan_limit("Active connection limit reached for the free plan.")
