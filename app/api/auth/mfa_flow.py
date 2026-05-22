"""Post-credential auth branching — tokens, MFA challenge, or forced 2FA setup."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.api.errors import PulseHTTPException, service_unavailable
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.org_helpers import is_org_owner
from app.api.schemas.auth import MfaRequiredResponse, TokenResponse
from app.infrastructure.database.models.organization import Organization
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.organization_repository import OrganizationRepository
from app.infrastructure.redis import mfa_tokens as redis_mfa
from app.infrastructure.redis.client import get_redis
from app.services.totp_service import user_totp_enabled


def _user_dict(u: User, *, is_org_owner_flag: bool = False) -> dict[str, Any]:
    return {
        "id": str(u.id),
        "email": u.email,
        "full_name": u.full_name,
        "role": u.role,
        "is_verified": u.is_verified,
        "is_active": u.is_active,
        "org_id": str(u.org_id),
        "profile_image_url": u.profile_image_url,
        "totp_enabled": user_totp_enabled(u),
        "is_org_owner": is_org_owner_flag,
    }


def _org_dict(o: Organization) -> dict[str, Any]:
    return {
        "id": str(o.id),
        "name": o.name,
        "slug": o.slug,
        "industry": o.industry,
        "business_context": o.business_context,
        "entity_label": o.entity_label,
        "goal_label": o.goal_label,
        "plan": o.plan,
        "timezone": o.timezone,
        "onboarding_done": o.onboarding_done,
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "updated_at": o.updated_at.isoformat() if o.updated_at else None,
        "logo_url": o.logo_url,
        "tour_guide": getattr(o, "tour_guide", None) or {},
        "require_2fa": bool(getattr(o, "require_2fa", False)),
    }


async def resolve_post_auth(
    db: AsyncSession,
    user: User,
    *,
    issue_tokens,
) -> TokenResponse | MfaRequiredResponse:
    """Return tokens, MFA challenge, or forced 2FA setup after credentials are verified."""
    org = await OrganizationRepository(db).get_by_id(user.org_id)
    if org and org.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "ORG_DELETED", "message": "This organization has been deleted"},
        )

    owner_flag = is_org_owner(user, org)

    if user_totp_enabled(user):
        if await get_redis() is None:
            raise service_unavailable(
                "REDIS_REQUIRED",
                "Two-factor authentication requires Redis. Please try again later.",
            )
        mfa_token = await redis_mfa.create_mfa_login_token(user_id=user.id, org_id=user.org_id)
        return MfaRequiredResponse(
            status="mfa_required",
            mfa_token=mfa_token,
            user=_user_dict(user, is_org_owner_flag=owner_flag),
        )

    if org and org.require_2fa:
        if await get_redis() is None:
            raise service_unavailable(
                "REDIS_REQUIRED",
                "Organization two-factor policy requires Redis. Please try again later.",
            )
        setup_token = await redis_mfa.create_mfa_setup_token(user_id=user.id)
        raise PulseHTTPException(
            status.HTTP_403_FORBIDDEN,
            code="TWO_FACTOR_SETUP_REQUIRED",
            message="Your organization requires two-factor authentication. Set it up to continue.",
            fields={"setup_token": setup_token},
        )

    access, refresh = await issue_tokens(user, user.org_id)
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        user=_user_dict(user, is_org_owner_flag=owner_flag),
        org=_org_dict(org) if org else None,
    )
