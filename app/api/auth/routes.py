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
    decode_access_token,
    parse_uuid_loose,
    parse_uuid_sub,
)
from app.api.auth.mfa_flow import _org_dict, _user_dict, resolve_post_auth
from app.api.auth.org_helpers import is_org_owner
from app.api.auth.token_utils import issue_tokens
from app.api.auth.passwords import hash_password, verify_password
from app.api.errors import bad_request, conflict, not_found, rate_limited, unauthorized
from app.api.schemas.auth import (
    AcceptInviteRequest,
    ForgotPasswordRequest,
    GoogleCompleteSignupRequest,
    GoogleLinkCancelRequest,
    GoogleLinkRequest,
    InvitePreviewResponse,
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
from app.api.schemas.auth import MfaRequiredResponse
from app.config.settings import settings
from app.infrastructure.database.models.invitation import Invitation
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.organization_repository import (
    OrganizationRepository,
)
from app.infrastructure.database.repositories.user_repository import UserRepository
from app.infrastructure.audit import log_audit, request_audit_context
from app.infrastructure.database.session import get_db
from app.services.email_queue import queue_email
from app.services.notification_service import notify_member_joined
from app.infrastructure.redis import keys as redis_keys
from app.infrastructure.redis.client import get_redis
from app.infrastructure.redis import tokens as redis_tokens
from app.infrastructure.redis.rate_limit import (
    ACCEPT_INVITE_IP_PER_MIN,
    FORGOT_PASSWORD_EMAIL_PER_HOUR,
    FORGOT_PASSWORD_IP_PER_MIN,
    LOGIN_IP_PER_MIN,
    OAUTH_START_IP_PER_MIN,
    REFRESH_IP_PER_MIN,
    RESET_PASSWORD_IP_PER_MIN,
    SIGNUP_EMAIL_PER_HOUR,
    SIGNUP_IP_PER_MIN,
    VERIFY_EMAIL_IP_PER_MIN,
    enforce_auth_email_limit,
    enforce_auth_ip_limit,
)
from app.services.self_hosted_instance import (
    ensure_can_create_organization,
    instance_registration_open,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["Auth"])


async def _issue_tokens(user: User, org_id: UUID) -> tuple[str, str]:
    return await issue_tokens(user, org_id)


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
    r = await get_redis()
    await enforce_auth_ip_limit(
        r, request, "signup", limit=SIGNUP_IP_PER_MIN,
        message="Too many signup attempts from this network. Try again shortly.",
    )
    await enforce_auth_email_limit(
        r, str(body.email), "signup", limit=SIGNUP_EMAIL_PER_HOUR, window_sec=3600,
        message="Too many signup attempts for this email. Try again later.",
    )
    await ensure_can_create_organization(db)
    existing = await UserRepository(db).get_by_email(body.email)
    if existing:
        raise conflict("EMAIL_TAKEN", "Email already registered")

    org = await OrganizationRepository(db).create(body.org_name)
    org.owner_user_id = None  # set after user flush
    user = await UserRepository(db).create(
        org_id=org.id,
        email=body.email,
        password_hash=hash_password(body.password),
        role="admin",
    )
    user.full_name = body.full_name or ""
    user.is_verified = False
    org.owner_user_id = user.id
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
            await queue_email("verification", to=user.email, token=token)
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


@router.post("/login", response_model=TokenResponse | MfaRequiredResponse)
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    r = await get_redis()
    await enforce_auth_ip_limit(
        r, request, "login", limit=LOGIN_IP_PER_MIN,
        message="Too many login attempts. Try again in a minute.",
    )
    await enforce_auth_email_limit(
        r, str(body.email), "login", limit=LOGIN_IP_PER_MIN, window_sec=60,
        message="Too many login attempts for this email. Try again in a minute.",
    )
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

    return await resolve_post_auth(db, user, issue_tokens=_issue_tokens)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    body: RefreshRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    r = await get_redis()
    await enforce_auth_ip_limit(
        r, request, "refresh", limit=REFRESH_IP_PER_MIN,
        message="Too many token refresh attempts. Try again shortly.",
    )
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
async def verify_email(
    request: Request,
    token: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    r = await get_redis()
    await enforce_auth_ip_limit(
        r, request, "verify_email", limit=VERIFY_EMAIL_IP_PER_MIN,
        message="Too many verification attempts. Try again shortly.",
    )
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
    rate_key = redis_keys.email_verify_rate(current_user.id)
    if await r.get(rate_key):
        raise rate_limited("Please wait before requesting another verification email")
    await r.set(rate_key, "1", ex=60)
    token = await redis_tokens.set_email_verify_token(current_user.id)
    await queue_email("verification", to=current_user.email, token=token)
    return {"message": "Verification email sent"}


@router.post("/forgot-password")
async def forgot_password(
    body: ForgotPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    r = await get_redis()
    await enforce_auth_ip_limit(
        r, request, "forgot_password", limit=FORGOT_PASSWORD_IP_PER_MIN,
        message="Too many password reset requests. Try again shortly.",
    )
    await enforce_auth_email_limit(
        r, str(body.email), "forgot_password",
        limit=FORGOT_PASSWORD_EMAIL_PER_HOUR, window_sec=3600,
        message="Too many password reset requests for this email. Try again later.",
    )
    user = await UserRepository(db).get_by_email(body.email)
    if user and user.is_active and await get_redis() is not None:
        try:
            token = await redis_tokens.set_pw_reset_token(user.id)
            await queue_email("password_reset", to=user.email, token=token)
        except Exception:
            logger.exception("pw reset email enqueue failed for %s", body.email)
    return {"message": "If that email exists, a reset link has been sent"}


@router.post("/reset-password")
async def reset_password(
    body: ResetPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    r = await get_redis()
    await enforce_auth_ip_limit(
        r, request, "reset_password", limit=RESET_PASSWORD_IP_PER_MIN,
        message="Too many password reset attempts. Try again shortly.",
    )
    if r is None:
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
    return await _oauth_post_auth_redirect(user, dest, db)


async def _get_valid_invitation(db: AsyncSession, token: str) -> Invitation | None:
    if not (token or "").strip():
        return None
    result = await db.execute(select(Invitation).where(Invitation.token == token.strip()))
    inv = result.scalar_one_or_none()
    if not inv or inv.accepted_at is not None:
        return None
    if inv.expires_at < datetime.now(timezone.utc):
        return None
    return inv


async def _oauth_post_auth_redirect(user: User, dest: str, db: AsyncSession) -> RedirectResponse:
    try:
        result = await resolve_post_auth(db, user, issue_tokens=_issue_tokens)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        setup_tok = detail.get("setup_token") or (detail.get("fields") or {}).get("setup_token")
        if detail.get("code") == "TWO_FACTOR_SETUP_REQUIRED" and setup_tok:
            return _oauth_redirect(
                dest,
                oauth_action="setup_2fa",
                setup_token=str(setup_tok),
            )
        raise
    if isinstance(result, MfaRequiredResponse):
        return _oauth_redirect(
            dest,
            oauth_action="mfa_required",
            mfa_token=result.mfa_token,
        )
    return _oauth_redirect(
        dest,
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        token_type="bearer",
        oauth_action="login",
    )


async def _oauth_accept_invite_redirect(
    *,
    db: AsyncSession,
    dest: str,
    inv: Invitation,
    email: str,
    sub: str,
    name: str,
    picture: str | None,
) -> RedirectResponse:
    invite_qs = {"invite_token": inv.token}
    if email != (inv.email or "").strip().lower():
        return _oauth_redirect(
            dest,
            error="invite_email_mismatch",
            code="INVITE_EMAIL_MISMATCH",
            **invite_qs,
        )

    existing = await UserRepository(db).get_by_email(inv.email)
    if existing:
        return _oauth_redirect(
            dest,
            error="account_exists",
            code="ALREADY_IN_ORG",
            **invite_qs,
        )

    user = await UserRepository(db).create(
        org_id=inv.org_id,
        email=inv.email,
        password_hash=None,
        role=inv.role,
    )
    user.full_name = name or user.full_name
    user.profile_image_url = picture or user.profile_image_url
    user.auth_provider = "google"
    user.auth_provider_id = sub or None
    user.is_verified = True
    user.last_login_at = datetime.now(timezone.utc)
    inv.accepted_at = datetime.now(timezone.utc)
    await log_audit(
        db,
        org_id=inv.org_id,
        user_id=user.id,
        action="user.invite_accepted",
        resource="invitation",
        resource_id=inv.id,
        metadata={"email": user.email, "role": user.role, "auth_provider": "google"},
    )
    await notify_member_joined(
        db,
        inv.org_id,
        user_id=user.id,
        user_name=user.full_name,
        user_email=user.email,
        role=user.role,
    )
    await db.commit()
    await db.refresh(user)
    return await _oauth_post_auth_redirect(user, dest, db)


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


@router.get("/invite/preview", response_model=InvitePreviewResponse)
async def invite_preview(
    request: Request,
    token: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
) -> InvitePreviewResponse:
    """Public preview of a pending invitation (email, org name, role)."""
    r = await get_redis()
    await enforce_auth_ip_limit(
        r, request, "invite_preview", limit=30,
        message="Too many requests. Try again shortly.",
    )
    inv = await _get_valid_invitation(db, token)
    if not inv:
        raise not_found("Invitation not found or expired")
    org = await OrganizationRepository(db).get_by_id(inv.org_id)
    if not org:
        raise not_found("Organization not found")
    return InvitePreviewResponse(
        email=inv.email,
        org_name=org.name,
        role=inv.role,
        expires_at=inv.expires_at.isoformat(),
    )


@router.get("/oauth/google")
async def oauth_google_start(
    request: Request,
    redirect_uri: str | None = Query(None),
    intent: str = Query("login"),
    invite_token: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    r = await get_redis()
    await enforce_auth_ip_limit(
        r, request, "oauth_google", limit=OAUTH_START_IP_PER_MIN,
        message="Too many sign-in attempts. Try again shortly.",
    )
    if not settings.is_google_oauth_configured():
        raise HTTPException(
            status_code=501,
            detail={"code": "NOT_CONFIGURED", "message": "Google OAuth not configured"},
        )
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
    oauth_intent = intent
    invite_token_clean = (invite_token or "").strip()
    if oauth_intent == "invite":
        if not invite_token_clean:
            raise bad_request("BAD_REQUEST", "invite_token is required for invite sign-in")
        inv = await _get_valid_invitation(db, invite_token_clean)
        if not inv:
            raise bad_request("INVALID_TOKEN", "Invalid or expired invitation")
    elif oauth_intent not in ("login", "signup"):
        oauth_intent = "login"
    state = secrets.token_urlsafe(32)
    state_payload: dict = {"dest": dest, "intent": oauth_intent}
    if oauth_intent == "invite":
        state_payload["invite_token"] = invite_token_clean
    await r.set(redis_keys.oauth_google_state(state), json.dumps(state_payload), ex=_OAUTH_PENDING_TTL)
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
    invite_token = (state_data.get("invite_token") or "").strip()

    try:
        info = await _fetch_google_profile(code)
    except ValueError as exc:
        logger.warning("Google OAuth profile fetch failed: %s", exc)
        return _oauth_redirect(dest, error="oauth_failed", code="OAUTH_FAILED")

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

    if intent == "invite":
        extra = {"invite_token": invite_token} if invite_token else {}
        if user is not None:
            return _oauth_redirect(
                dest,
                error="account_exists",
                code="ALREADY_IN_ORG",
                **extra,
            )
        inv = await _get_valid_invitation(db, invite_token)
        if not inv:
            return _oauth_redirect(dest, error="invite_invalid", code="INVALID_TOKEN", **extra)
        return await _oauth_accept_invite_redirect(
            db=db,
            dest=dest,
            inv=inv,
            email=email,
            sub=sub,
            name=name,
            picture=picture,
        )

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
    return await resolve_post_auth(db, user, issue_tokens=_issue_tokens)


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
    org.owner_user_id = user.id
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
    return await resolve_post_auth(db, user, issue_tokens=_issue_tokens)


@router.post("/accept-invite", response_model=TokenResponse | MfaRequiredResponse)
async def accept_invite(
    body: AcceptInviteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse | MfaRequiredResponse:
    """Create user on ``Invitation.org_id`` with the role stored on the invitation row."""
    r = await get_redis()
    await enforce_auth_ip_limit(
        r, request, "accept_invite", limit=ACCEPT_INVITE_IP_PER_MIN,
        message="Too many invite acceptance attempts. Try again shortly.",
    )
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
    await notify_member_joined(
        db,
        inv.org_id,
        user_id=user.id,
        user_name=user.full_name,
        user_email=user.email,
        role=user.role,
    )
    await db.commit()
    await db.refresh(user)
    return await resolve_post_auth(db, user, issue_tokens=_issue_tokens)


@router.get("/me", response_model=MeResponse)
async def me(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MeResponse:
    org = await OrganizationRepository(db).get_by_id(current_user.org_id)
    if not org:
        raise not_found("Organization not found")
    from app.services.totp_service import user_totp_enabled

    owner_flag = is_org_owner(current_user, org)
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
            totp_enabled=user_totp_enabled(current_user),
            is_org_owner=owner_flag,
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
            require_2fa=bool(getattr(org, "require_2fa", False)),
        ),
    )


from app.api.auth.totp_routes import router as _totp_router

router.include_router(_totp_router)
