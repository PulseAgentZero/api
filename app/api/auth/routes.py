"""Authentication routes (BACKEND_ROUTES §1)."""

from __future__ import annotations

import logging
import secrets
import urllib.parse
from datetime import datetime, timezone
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.jwt_utils import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    parse_uuid_loose,
    parse_uuid_sub,
)
from app.api.auth.passwords import hash_password, verify_password
from app.api.errors import bad_request, conflict, not_found, unauthorized
from app.api.schemas.auth import (
    AcceptInviteRequest,
    ForgotPasswordRequest,
    LoginRequest,
    LogoutRequest,
    MeResponse,
    OrgOut,
    RefreshRequest,
    ResetPasswordRequest,
    SignupRequest,
    TokenResponse,
    UserOut,
)
from app.config.settings import settings
from app.infrastructure.database.models.invitation import Invitation
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.organization_repository import (
    OrganizationRepository,
)
from app.infrastructure.database.repositories.user_repository import UserRepository
from app.infrastructure.database.session import get_db
from app.infrastructure.email import send_password_reset_email, send_verification_email
from app.infrastructure.redis import keys as redis_keys
from app.infrastructure.redis.client import get_redis
from app.infrastructure.redis import tokens as redis_tokens

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["Auth"])


async def _issue_tokens(user: User, org_id: UUID) -> tuple[str, str]:
    """Mint access + refresh tokens.

    When Redis is available, refresh tokens are opaque server-side values
    (one-time rotation on /auth/refresh). When Redis is unavailable, refresh
    tokens are signed JWTs (stateless, reusable until expiry) — see CLAUDE.md.
    """
    access = create_access_token(user.id, org_id, user.role, user.email)
    r = await get_redis()
    if r is not None:
        refresh = await redis_tokens.set_refresh_token(user.id, org_id, user.role)
    else:
        refresh = create_refresh_token(user.id)
    return access, refresh


def _user_dict(u: User) -> dict:
    return {
        "id": str(u.id),
        "email": u.email,
        "full_name": u.full_name,
        "role": u.role,
        "is_verified": u.is_verified,
        "is_active": u.is_active,
        "org_id": str(u.org_id),
        "profile_image_url": u.profile_image_url,
    }


def _org_dict(o) -> dict:
    return {
        "id": str(o.id),
        "name": o.name,
        "slug": o.slug,
        "industry": o.industry,
        "plan": o.plan,
        "onboarding_done": o.onboarding_done,
        "logo_url": o.logo_url,
        "tour_guide": getattr(o, "tour_guide", None) or {},
    }


@router.post("/signup", response_model=TokenResponse, status_code=201)
async def signup(body: SignupRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    existing = await UserRepository(db).get_by_email(body.email)
    if existing:
        raise conflict("EMAIL_TAKEN", "Email already registered")

    org = await OrganizationRepository(db).create(body.org_name)
    user = await UserRepository(db).create(
        org_id=org.id,
        email=body.email,
        password_hash=hash_password(body.password),
        role="admin",
    )
    user.full_name = body.full_name or ""
    user.is_verified = False
    await db.commit()
    try:
        if await get_redis() is not None:
            token = await redis_tokens.set_email_verify_token(user.id)
            await send_verification_email(user.email, token)
    except Exception:
        logger.exception("verification email skipped for %s", user.email)
    await db.refresh(user)
    await db.refresh(org)

    access, refresh = await _issue_tokens(user, org.id)
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        user=_user_dict(user),
        org=_org_dict(org),
    )


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    user = await UserRepository(db).get_by_email(body.email)
    if not user or not user.password_hash or not verify_password(body.password, user.password_hash):
        raise unauthorized("INVALID_CREDENTIALS", "Invalid email or password")
    if not user.is_active:
        raise HTTPException(
            status_code=403,
            detail={"code": "ACCOUNT_DEACTIVATED", "message": "Account deactivated"},
        )
    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()

    access, refresh = await _issue_tokens(user, user.org_id)
    org = await OrganizationRepository(db).get_by_id(user.org_id)
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        user=_user_dict(user),
        org=_org_dict(org) if org else None,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    r = await get_redis()
    if r is not None:
        data = await redis_tokens.get_refresh_token(body.refresh_token)
        if not data:
            raise unauthorized("INVALID_TOKEN", "Invalid or expired refresh token")
        uid = parse_uuid_loose(data.get("user_id"))
        if uid is None:
            raise unauthorized("INVALID_TOKEN", "Invalid or expired refresh token")
        user = await UserRepository(db).get_by_id(uid)
        if not user or not user.is_active:
            raise HTTPException(
                status_code=403,
                detail={"code": "ACCOUNT_DEACTIVATED", "message": "Account deactivated"},
            )
        oid = parse_uuid_loose(data.get("org_id"))
        if oid is not None and oid != user.org_id:
            raise unauthorized("INVALID_TOKEN", "Refresh token organization mismatch")
        await redis_tokens.delete_refresh_token(body.refresh_token)
        access, new_refresh = await _issue_tokens(user, user.org_id)
        org = await OrganizationRepository(db).get_by_id(user.org_id)
        return TokenResponse(
            access_token=access,
            refresh_token=new_refresh,
            user=_user_dict(user),
            org=_org_dict(org) if org else None,
        )

    payload = decode_access_token(body.refresh_token)
    if payload is None or payload.get("type") != "refresh":
        raise unauthorized("INVALID_TOKEN", "Invalid or expired refresh token")
    uid = parse_uuid_sub(payload)
    if uid is None:
        raise unauthorized("INVALID_TOKEN", "Invalid or expired refresh token")
    user = await UserRepository(db).get_by_id(uid)
    if not user:
        raise unauthorized("INVALID_TOKEN", "User not found")
    access = create_access_token(user.id, user.org_id, user.role, user.email)
    new_refresh = create_refresh_token(user.id)
    org = await OrganizationRepository(db).get_by_id(user.org_id)
    return TokenResponse(
        access_token=access,
        refresh_token=new_refresh,
        user=_user_dict(user),
        org=_org_dict(org) if org else None,
    )


@router.post("/logout", status_code=204)
async def logout(
    body: LogoutRequest,
    current_user: User = Depends(get_current_user),
):
    r = await get_redis()
    if r is not None:
        await redis_tokens.delete_refresh_token(body.refresh_token)


@router.get("/verify-email")
async def verify_email(token: str = Query(...), db: AsyncSession = Depends(get_db)) -> dict:
    r = await get_redis()
    if r is None:
        raise bad_request("INVALID_TOKEN", "Verification unavailable")
    uid = await redis_tokens.get_email_verify_token(token)
    if not uid:
        raise bad_request("INVALID_TOKEN", "Token expired or not found")
    user_id = parse_uuid_loose(uid)
    if user_id is None:
        raise bad_request("INVALID_TOKEN", "Invalid token")
    user = await UserRepository(db).get_by_id(user_id)
    if not user:
        raise bad_request("INVALID_TOKEN", "Invalid token")
    if user.is_verified:
        raise bad_request("ALREADY_VERIFIED", "Email already verified")
    user.is_verified = True
    await redis_tokens.delete_email_verify_token(token)
    await db.commit()
    return {"message": "Email verified successfully"}


@router.post("/resend-verification")
async def resend_verification(current_user: User = Depends(get_current_user)) -> dict:
    if current_user.is_verified:
        raise bad_request("ALREADY_VERIFIED", "User is already verified")
    r = await get_redis()
    if r is None:
        return {
            "message": "Email verification requires Redis; it is not configured. "
            "Configure REDIS_URL to send verification emails.",
        }
    # Rate limit: block if a token was set in the last 60 seconds
    from app.infrastructure.redis.keys import email_verify_rate
    rate_key = email_verify_rate(current_user.id)
    if await r.get(rate_key):
        raise bad_request("RATE_LIMITED", "Please wait before requesting another verification email")
    await r.set(rate_key, "1", ex=60)
    token = await redis_tokens.set_email_verify_token(current_user.id)
    try:
        await send_verification_email(current_user.email, token)
    except Exception:
        logger.exception("resend verification email failed for %s", current_user.email)
    return {"message": "Verification email sent"}


@router.post("/forgot-password")
async def forgot_password(body: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)) -> dict:
    user = await UserRepository(db).get_by_email(body.email)
    if user and user.is_active and await get_redis() is not None:
        try:
            token = await redis_tokens.set_pw_reset_token(user.id)
            await send_password_reset_email(user.email, token)
        except Exception:
            logger.exception("pw reset email failed for %s", body.email)
    return {"message": "If that email exists, a reset link has been sent"}


@router.post("/reset-password")
async def reset_password(body: ResetPasswordRequest, db: AsyncSession = Depends(get_db)) -> dict:
    if await get_redis() is None:
        raise bad_request("INVALID_TOKEN", "Reset unavailable")
    uid = await redis_tokens.get_pw_reset_token(body.token)
    if not uid:
        raise bad_request("INVALID_TOKEN", "Token expired or not found")
    user_id = parse_uuid_loose(uid)
    if user_id is None:
        raise bad_request("INVALID_TOKEN", "Invalid token")
    user = await UserRepository(db).get_by_id(user_id)
    if not user:
        raise bad_request("INVALID_TOKEN", "Invalid token")
    user.password_hash = hash_password(body.new_password)
    await redis_tokens.delete_pw_reset_token(body.token)
    await db.commit()
    # Invalidate all active sessions so old tokens can't be reused
    r = await get_redis()
    if r is not None:
        from app.infrastructure.redis.keys import user_sessions_pattern
        pattern = user_sessions_pattern(user.id)
        async for key in r.scan_iter(match=pattern):
            await r.delete(key)
    return {"message": "Password updated successfully"}


@router.get("/oauth/google")
async def oauth_google_start(redirect_uri: str | None = Query(None)) -> RedirectResponse:
    if not settings.is_google_oauth_configured():
        raise HTTPException(
            status_code=501,
            detail={"code": "NOT_CONFIGURED", "message": "Google OAuth not configured"},
        )
    r = await get_redis()
    if r is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "REDIS_REQUIRED", "message": "Redis is required for Google OAuth state"},
        )
    state = secrets.token_urlsafe(32)
    dest = (redirect_uri or settings.FRONTEND_URL).strip() or settings.FRONTEND_URL
    await r.set(redis_keys.oauth_google_state(state), dest, ex=600)
    cid = settings.get_google_client_id()
    redir = settings.GOOGLE_REDIRECT_URI
    params = urllib.parse.urlencode(
        {
            "client_id": cid,
            "redirect_uri": redir,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")


@router.get("/oauth/google/callback")
async def oauth_google_callback(
    code: str = Query(...),
    state: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    if not settings.is_google_oauth_configured():
        raise HTTPException(status_code=501, detail={"code": "NOT_CONFIGURED", "message": "Google OAuth not configured"})
    r = await get_redis()
    if r is None or not state:
        raise HTTPException(status_code=400, detail={"code": "BAD_REQUEST", "message": "Missing OAuth state"})
    dest = await r.get(redis_keys.oauth_google_state(state))
    if not dest:
        raise bad_request("INVALID_STATE", "OAuth state expired or invalid")
    await r.delete(redis_keys.oauth_google_state(state))
    dest = dest.decode() if isinstance(dest, bytes) else str(dest)

    token_url = "https://oauth2.googleapis.com/token"
    cid = settings.get_google_client_id()
    sec = settings.get_google_client_secret()
    redir = settings.GOOGLE_REDIRECT_URI
    async with httpx.AsyncClient(timeout=30.0) as client:
        tr = await client.post(
            token_url,
            data={
                "code": code,
                "client_id": cid,
                "client_secret": sec,
                "redirect_uri": redir,
                "grant_type": "authorization_code",
            },
        )
        if tr.status_code != 200:
            logger.warning("Google token exchange failed: %s %s", tr.status_code, tr.text[:300])
            raise bad_request("OAUTH_TOKEN", "Google token exchange failed")
        tokens = tr.json()
        g_access = tokens.get("access_token")
        if not g_access:
            raise bad_request("OAUTH_TOKEN", "No access token from Google")
        ui = await client.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {g_access}"},
        )
        if ui.status_code != 200:
            raise bad_request("OAUTH_PROFILE", "Failed to load Google profile")
        info = ui.json()
    email = (info.get("email") or "").strip().lower()
    if not email:
        raise bad_request("OAUTH_EMAIL", "Google did not return an email")
    sub = str(info.get("sub") or "")
    picture = (info.get("picture") or "").strip() or None
    name = (info.get("name") or email.split("@")[0]).strip()

    user = await UserRepository(db).get_by_email(email)
    if user:
        user.profile_image_url = picture or user.profile_image_url
        user.auth_provider = "google"
        if sub:
            user.auth_provider_id = sub
        user.last_login_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(user)
        access_jwt, refresh_jwt = await _issue_tokens(user, user.org_id)
    else:
        org = await OrganizationRepository(db).create(f"{name}'s workspace")
        user = await UserRepository(db).create(
            org_id=org.id,
            email=email,
            password_hash=None,
            role="admin",
        )
        user.full_name = name
        user.profile_image_url = picture
        user.auth_provider = "google"
        user.auth_provider_id = sub or None
        user.is_verified = True
        user.last_login_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(user)
        await db.refresh(org)
        access_jwt, refresh_jwt = await _issue_tokens(user, org.id)

    q = urllib.parse.urlencode(
        {
            "access_token": access_jwt,
            "refresh_token": refresh_jwt,
            "token_type": "bearer",
        }
    )
    join = "&" if "?" in dest else "?"
    return RedirectResponse(f"{dest}{join}{q}", status_code=status.HTTP_302_FOUND)


@router.post("/accept-invite", response_model=TokenResponse)
async def accept_invite(body: AcceptInviteRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    """Create user on ``Invitation.org_id`` with the role stored on the invitation row."""
    result = await db.execute(select(Invitation).where(Invitation.token == body.token))
    inv = result.scalar_one_or_none()
    if not inv or inv.accepted_at is not None:
        raise bad_request("INVALID_TOKEN", "Invalid or expired invitation")
    if inv.expires_at < datetime.now(timezone.utc):
        raise bad_request("INVALID_TOKEN", "Invitation expired")
    existing = await UserRepository(db).get_by_email(inv.email)
    if existing:
        raise conflict("ALREADY_IN_ORG", "User already exists")
    user = await UserRepository(db).create(
        org_id=inv.org_id,
        email=inv.email,
        password_hash=hash_password(body.password),
        role=inv.role,
    )
    user.full_name = body.full_name
    user.is_verified = True
    inv.accepted_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(user)
    org = await OrganizationRepository(db).get_by_id(inv.org_id)
    access, refresh = await _issue_tokens(user, inv.org_id)
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        user=_user_dict(user),
        org=_org_dict(org) if org else None,
    )


@router.get("/me", response_model=MeResponse)
async def me(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MeResponse:
    org = await OrganizationRepository(db).get_by_id(current_user.org_id)
    if not org:
        raise not_found("Organization not found")
    return MeResponse(
        user=UserOut(
            id=current_user.id,
            email=current_user.email,
            full_name=current_user.full_name,
            role=current_user.role,
            is_verified=current_user.is_verified,
            is_active=current_user.is_active,
            last_login_at=current_user.last_login_at.isoformat() if current_user.last_login_at else None,
            created_at=current_user.created_at.isoformat(),
            org_id=current_user.org_id,
            profile_image_url=current_user.profile_image_url,
        ),
        org=OrgOut(
            id=org.id,
            name=org.name,
            slug=org.slug,
            industry=org.industry,
            plan=org.plan,
            onboarding_done=org.onboarding_done,
            created_at=org.created_at.isoformat(),
            logo_url=org.logo_url,
            tour_guide=getattr(org, "tour_guide", None) or {},
        ),
    )
