"""Authentication routes (BACKEND_ROUTES §1)."""

from __future__ import annotations

import json
import logging
import secrets
import urllib.parse
from datetime import datetime, timezone
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
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
    GoogleCompleteSignupRequest,
    GoogleLinkCancelRequest,
    GoogleLinkRequest,
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
from app.infrastructure.audit import log_audit, request_audit_context
from app.infrastructure.database.session import get_db
from app.infrastructure.email import (
    send_password_reset_email,
    send_verification_email,
    send_welcome_email,
)
from app.infrastructure.redis import keys as redis_keys
from app.infrastructure.redis.client import get_redis
from app.infrastructure.redis import tokens as redis_tokens
from app.services.self_hosted_instance import (
    ensure_can_create_organization,
    instance_registration_open,
)

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


@router.get("/instance")
async def auth_instance_status(db: AsyncSession = Depends(get_db)) -> dict:
    """Public instance metadata (registration gates, deployment mode)."""
    open_ = await instance_registration_open(db)
    return {
        "deployment_mode": settings.DEPLOYMENT_MODE,
        "registration_open": open_,
        "can_create_organization": open_,
    }


@router.post("/signup", response_model=TokenResponse, status_code=201)
async def signup(
    body: SignupRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    await ensure_can_create_organization(db)
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
    ip, ua = request_audit_context(request)
    await log_audit(
        db,
        org_id=org.id,
        user_id=user.id,
        action="org.created",
        resource="organization",
        resource_id=org.id,
        metadata={"name": org.name},
        ip_address=ip,
        user_agent=ua,
    )
    await log_audit(
        db,
        org_id=org.id,
        user_id=user.id,
        action="user.signup",
        resource="user",
        resource_id=user.id,
        metadata={"email": user.email, "role": user.role},
        ip_address=ip,
        user_agent=ua,
    )
    await db.commit()
    try:
        if await get_redis() is not None:
            token = await redis_tokens.set_email_verify_token(user.id)
            await send_verification_email(user.email, token)
    except Exception:
        logger.exception("verification email skipped for %s", user.email)
    try:
        await send_welcome_email(
            user.email,
            full_name=user.full_name or "",
            org_name=org.name,
        )
    except Exception:
        logger.exception("welcome email skipped for %s", user.email)
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
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    user = await UserRepository(db).get_by_email(body.email)
    if not user or not user.password_hash or not verify_password(body.password, user.password_hash):
        raise unauthorized("INVALID_CREDENTIALS", "Invalid email or password")
    if not user.is_active:
        raise HTTPException(
            status_code=403,
            detail={"code": "ACCOUNT_DEACTIVATED", "message": "Account deactivated"},
        )
    user.last_login_at = datetime.now(timezone.utc)
    ip, ua = request_audit_context(request)
    await log_audit(
        db,
        org_id=user.org_id,
        user_id=user.id,
        action="user.login",
        resource="user",
        resource_id=user.id,
        metadata={"email": user.email},
        ip_address=ip,
        user_agent=ua,
    )
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
    if not user.is_verified:
        user.is_verified = True
        await db.commit()
    await redis_tokens.delete_email_verify_token(token)
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


_OAUTH_PENDING_TTL = 600


def _oauth_redirect(dest: str, **params: str) -> RedirectResponse:
    filtered = {k: v for k, v in params.items() if v is not None and v != ""}
    q = urllib.parse.urlencode(filtered)
    join = "&" if "?" in dest else "?"
    url = f"{dest}{join}{q}" if q else dest
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


def _parse_oauth_state(raw: str | bytes) -> dict:
    text = raw.decode() if isinstance(raw, bytes) else str(raw)
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data.get("dest"):
            return data
    except json.JSONDecodeError:
        pass
    return {"dest": text, "intent": "login"}


def _google_profile_linked(user: User, sub: str) -> bool:
    return user.auth_provider == "google" and bool(sub) and user.auth_provider_id == sub


def _needs_link_consent(user: User, sub: str) -> bool:
    if not user.password_hash:
        return False
    return not _google_profile_linked(user, sub)


async def _fetch_google_profile(code: str) -> dict:
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
            raise ValueError("OAUTH_TOKEN")
        tokens = tr.json()
        g_access = tokens.get("access_token")
        if not g_access:
            raise ValueError("OAUTH_TOKEN")
        ui = await client.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {g_access}"},
        )
        if ui.status_code != 200:
            raise ValueError("OAUTH_PROFILE")
        return ui.json()


async def _oauth_login_redirect(
    user: User,
    dest: str,
    db: AsyncSession,
    *,
    picture: str | None,
    sub: str,
    name: str,
) -> RedirectResponse:
    user.profile_image_url = picture or user.profile_image_url
    if name and not user.full_name:
        user.full_name = name
    user.auth_provider = "google"
    if sub:
        user.auth_provider_id = sub
    user.is_verified = True
    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(user)
    access_jwt, refresh_jwt = await _issue_tokens(user, user.org_id)
    return _oauth_redirect(
        dest,
        access_token=access_jwt,
        refresh_token=refresh_jwt,
        token_type="bearer",
        oauth_action="login",
    )


async def _load_pending_json(r, key: str) -> dict | None:
    raw = await r.get(key)
    if not raw:
        return None
    text = raw.decode() if isinstance(raw, bytes) else str(raw)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


@router.get("/oauth/google")
async def oauth_google_start(
    redirect_uri: str | None = Query(None),
    intent: str = Query("login"),
) -> RedirectResponse:
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
    dest = (redirect_uri or settings.default_oauth_callback_dest()).strip()
    if not settings.is_oauth_redirect_allowed(dest):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_REDIRECT", "message": "redirect_uri origin is not allowed"},
        )
    oauth_intent = intent if intent in ("login", "signup") else "login"
    state = secrets.token_urlsafe(32)
    payload = json.dumps({"dest": dest, "intent": oauth_intent})
    await r.set(redis_keys.oauth_google_state(state), payload, ex=_OAUTH_PENDING_TTL)
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
    fallback_dest = settings.default_oauth_callback_dest()
    if not settings.is_google_oauth_configured():
        return _oauth_redirect(fallback_dest, error="oauth_failed", code="NOT_CONFIGURED")
    r = await get_redis()
    if r is None or not state:
        return _oauth_redirect(fallback_dest, error="oauth_failed", code="REDIS_REQUIRED")
    raw_state = await r.get(redis_keys.oauth_google_state(state))
    if not raw_state:
        return _oauth_redirect(fallback_dest, error="oauth_failed", code="INVALID_STATE")
    await r.delete(redis_keys.oauth_google_state(state))
    state_data = _parse_oauth_state(raw_state)
    dest = state_data.get("dest") or fallback_dest
    intent = state_data.get("intent") or "login"

    try:
        info = await _fetch_google_profile(code)
    except ValueError as exc:
        code_str = str(exc)
        return _oauth_redirect(dest, error="oauth_failed", code=code_str)

    email = (info.get("email") or "").strip().lower()
    if not email:
        return _oauth_redirect(dest, error="oauth_failed", code="OAUTH_EMAIL")
    sub = str(info.get("sub") or "")
    picture = (info.get("picture") or "").strip() or None
    name = (info.get("name") or email.split("@")[0]).strip()

    repo = UserRepository(db)
    user = await repo.get_by_email(email)
    if user is None and sub:
        user = await repo.get_by_auth_provider_id("google", sub)

    if user is not None:
        if not user.is_active:
            return _oauth_redirect(dest, error="account_deactivated", code="ACCOUNT_DEACTIVATED")
        if sub and user.auth_provider_id and user.auth_provider_id != sub:
            return _oauth_redirect(dest, error="oauth_account_conflict", code="OAUTH_ACCOUNT_CONFLICT")
        if _needs_link_consent(user, sub):
            link_token = secrets.token_urlsafe(32)
            pending = {
                "user_id": str(user.id),
                "email": email,
                "sub": sub,
                "name": name,
                "picture": picture,
                "dest": dest,
                "intent": intent,
            }
            await r.set(
                redis_keys.oauth_google_link_pending(link_token),
                json.dumps(pending),
                ex=_OAUTH_PENDING_TTL,
            )
            return _oauth_redirect(
                dest,
                oauth_action="link_account",
                link_token=link_token,
                email=email,
            )
        return await _oauth_login_redirect(user, dest, db, picture=picture, sub=sub, name=name)

    if not await instance_registration_open(db):
        return _oauth_redirect(
            dest,
            error="instance_org_exists",
            code="INSTANCE_ORG_EXISTS",
        )

    pending_token = secrets.token_urlsafe(32)
    signup_pending = {
        "email": email,
        "sub": sub,
        "name": name,
        "picture": picture,
        "dest": dest,
        "intent": intent,
    }
    await r.set(
        redis_keys.oauth_google_signup_pending(pending_token),
        json.dumps(signup_pending),
        ex=_OAUTH_PENDING_TTL,
    )
    return _oauth_redirect(
        dest,
        oauth_action="complete_signup",
        pending_token=pending_token,
    )


@router.post("/oauth/google/link", response_model=TokenResponse)
async def oauth_google_link(
    body: GoogleLinkRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    r = await get_redis()
    if r is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "REDIS_REQUIRED", "message": "Redis is required for Google OAuth"},
        )
    key = redis_keys.oauth_google_link_pending(body.link_token)
    pending = await _load_pending_json(r, key)
    if not pending:
        raise bad_request("INVALID_TOKEN", "Link request expired or invalid")
    user_id = parse_uuid_loose(pending.get("user_id"))
    if user_id is None:
        raise bad_request("INVALID_TOKEN", "Invalid link request")
    user = await UserRepository(db).get_by_id(user_id)
    if not user or not user.is_active:
        raise bad_request("INVALID_TOKEN", "Invalid link request")
    sub = str(pending.get("sub") or "")
    if sub and user.auth_provider_id and user.auth_provider_id != sub:
        raise conflict("OAUTH_ACCOUNT_CONFLICT", "This account is linked to a different Google account")
    if user.password_hash:
        if not body.password or not verify_password(body.password, user.password_hash):
            raise unauthorized("INVALID_CREDENTIALS", "Password is required to link your Google account")
    user.auth_provider = "google"
    if sub:
        user.auth_provider_id = sub
    picture = pending.get("picture")
    if picture:
        user.profile_image_url = picture
    name = pending.get("name")
    if name and not user.full_name:
        user.full_name = str(name)
    user.is_verified = True
    user.last_login_at = datetime.now(timezone.utc)
    await r.delete(key)
    await db.commit()
    await db.refresh(user)
    org = await OrganizationRepository(db).get_by_id(user.org_id)
    access, refresh = await _issue_tokens(user, user.org_id)
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        user=_user_dict(user),
        org=_org_dict(org) if org else None,
    )


@router.post("/oauth/google/link/cancel", status_code=204)
async def oauth_google_link_cancel(body: GoogleLinkCancelRequest) -> None:
    r = await get_redis()
    if r is not None:
        await r.delete(redis_keys.oauth_google_link_pending(body.link_token))


@router.post("/oauth/google/complete-signup", response_model=TokenResponse, status_code=201)
async def oauth_google_complete_signup(
    body: GoogleCompleteSignupRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    r = await get_redis()
    if r is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "REDIS_REQUIRED", "message": "Redis is required for Google OAuth"},
        )
    key = redis_keys.oauth_google_signup_pending(body.pending_token)
    pending = await _load_pending_json(r, key)
    if not pending:
        raise bad_request("INVALID_TOKEN", "Signup session expired or invalid")
    email = (pending.get("email") or "").strip().lower()
    if not email:
        raise bad_request("INVALID_TOKEN", "Invalid signup session")
    await ensure_can_create_organization(db)
    existing = await UserRepository(db).get_by_email(email)
    if existing:
        await r.delete(key)
        raise conflict("EMAIL_TAKEN", "Email already registered")
    sub = str(pending.get("sub") or "")
    name = (body.full_name or pending.get("name") or email.split("@")[0]).strip()
    picture = pending.get("picture")
    org = await OrganizationRepository(db).create(body.org_name.strip())
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
    await r.delete(key)
    await log_audit(
        db,
        org_id=org.id,
        user_id=user.id,
        action="org.created",
        resource="organization",
        resource_id=org.id,
        metadata={"name": org.name, "auth_provider": "google"},
    )
    await log_audit(
        db,
        org_id=org.id,
        user_id=user.id,
        action="user.signup",
        resource="user",
        resource_id=user.id,
        metadata={"email": user.email, "auth_provider": "google"},
    )
    await db.commit()
    await db.refresh(user)
    await db.refresh(org)
    try:
        await send_welcome_email(
            user.email,
            full_name=user.full_name or "",
            org_name=org.name,
        )
    except Exception:
        logger.exception("welcome email skipped for %s", user.email)
    access, refresh = await _issue_tokens(user, org.id)
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        user=_user_dict(user),
        org=_org_dict(org),
    )


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
    await log_audit(
        db,
        org_id=inv.org_id,
        user_id=user.id,
        action="user.invite_accepted",
        resource="invitation",
        resource_id=inv.id,
        metadata={"email": user.email, "role": user.role},
    )
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
