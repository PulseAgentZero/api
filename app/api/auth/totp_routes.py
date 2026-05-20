"""TOTP two-factor authentication routes."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user, get_current_user_optional
from app.api.auth.jwt_utils import parse_uuid_loose
from app.api.auth.mfa_flow import _org_dict, _user_dict, resolve_post_auth
from app.api.auth.org_helpers import is_org_owner
from app.api.auth.token_utils import issue_tokens
from app.api.errors import bad_request, unauthorized
from app.api.schemas.auth import (
    MfaVerifyRequest,
    TokenResponse,
    TotpDisableRequest,
    TotpEnableRequest,
    TotpSetupResponse,
)
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.organization_repository import OrganizationRepository
from app.infrastructure.database.repositories.user_repository import UserRepository
from app.infrastructure.database.session import get_db
from app.infrastructure.redis import mfa_tokens as redis_mfa
from app.infrastructure.redis.rate_limit import enforce_auth_ip_limit
from app.infrastructure.redis.client import get_redis
from app.services.totp_service import (
    begin_totp_setup,
    disable_totp,
    enable_totp,
    user_totp_enabled,
    verify_totp_code,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Auth"])


async def _user_from_setup_token(
    x_setup_token: str | None = Header(None, alias="X-Setup-Token"),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not x_setup_token:
        raise unauthorized("UNAUTHORIZED", "Setup token required")
    uid_raw = await redis_mfa.get_mfa_setup_user_id(x_setup_token)
    if not uid_raw:
        raise unauthorized("INVALID_TOKEN", "Invalid or expired setup token")
    uid = parse_uuid_loose(uid_raw)
    if uid is None:
        raise unauthorized("INVALID_TOKEN", "Invalid or expired setup token")
    user = await UserRepository(db).get_by_id(uid)
    if not user or not user.is_active:
        raise unauthorized("INVALID_TOKEN", "Invalid or expired setup token")
    return user


async def _actor_for_2fa_setup(
    x_setup_token: str | None = Header(None, alias="X-Setup-Token"),
    current_user: User | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
) -> User:
    if x_setup_token:
        return await _user_from_setup_token(x_setup_token, db)
    if current_user is None:
        raise unauthorized("UNAUTHORIZED", "Authentication required")
    return current_user


@router.post("/2fa/setup", response_model=TotpSetupResponse)
async def totp_setup(
    user: User = Depends(_actor_for_2fa_setup),
    db: AsyncSession = Depends(get_db),
) -> TotpSetupResponse:
    secret, uri = await begin_totp_setup(user)
    await db.commit()
    return TotpSetupResponse(secret=secret, otpauth_uri=uri)


@router.post("/2fa/enable")
async def totp_enable(
    body: TotpEnableRequest,
    user: User = Depends(_actor_for_2fa_setup),
    x_setup_token: str | None = Header(None, alias="X-Setup-Token"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    recovery_codes = await enable_totp(user, body.code)
    if x_setup_token:
        await redis_mfa.delete_mfa_setup_token(x_setup_token)
    await db.commit()
    payload: dict = {
        "message": "Two-factor authentication enabled",
        "recovery_codes": recovery_codes,
        "totp_enabled": True,
    }
    if x_setup_token:
        org = await OrganizationRepository(db).get_by_id(user.org_id)
        access, refresh = await issue_tokens(user, user.org_id)
        payload["access_token"] = access
        payload["refresh_token"] = refresh
        payload["user"] = _user_dict(user, is_org_owner_flag=is_org_owner(user, org))
        payload["org"] = _org_dict(org) if org else None
    return payload


@router.post("/2fa/disable")
async def totp_disable(
    body: TotpDisableRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org = await OrganizationRepository(db).get_by_id(current_user.org_id)
    await disable_totp(current_user, org, code=body.code, password=body.password)
    await db.commit()
    return {"message": "Two-factor authentication disabled", "totp_enabled": False}


@router.post("/2fa/verify", response_model=TokenResponse)
async def totp_verify_login(
    body: MfaVerifyRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    r = await get_redis()
    await enforce_auth_ip_limit(
        r,
        request,
        "mfa_verify",
        limit=30,
        message="Too many verification attempts. Try again shortly.",
    )
    data = await redis_mfa.get_mfa_login_token(body.mfa_token)
    if not data:
        raise unauthorized("INVALID_TOKEN", "Invalid or expired verification session")
    uid = parse_uuid_loose(data.get("user_id"))
    if uid is None:
        raise unauthorized("INVALID_TOKEN", "Invalid or expired verification session")
    user = await UserRepository(db).get_by_id(uid)
    if not user or not user.is_active:
        raise unauthorized("INVALID_TOKEN", "Invalid or expired verification session")
    if not user_totp_enabled(user) or not verify_totp_code(user, body.code):
        raise unauthorized("INVALID_TOTP", "Invalid verification code")
    await redis_mfa.delete_mfa_login_token(body.mfa_token)
    org = await OrganizationRepository(db).get_by_id(user.org_id)
    access, refresh = await issue_tokens(user, user.org_id)
    await db.commit()
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        user=_user_dict(user, is_org_owner_flag=is_org_owner(user, org)),
        org=_org_dict(org) if org else None,
    )
