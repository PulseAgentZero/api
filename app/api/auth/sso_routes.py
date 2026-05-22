"""Self-hosted SSO login (OIDC + SAML) — license feature `sso`."""

from __future__ import annotations

import json
import logging
import secrets
import urllib.parse
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx
import jwt
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.token_utils import issue_tokens
from app.api.dependencies.plan_gate import require_feature
from app.api.errors import bad_request, not_found
from app.config.settings import settings
from app.infrastructure.crypto import decrypt_secret
from app.infrastructure.database.models.organization import Organization
from app.infrastructure.database.models.sso_configuration import SsoConfiguration
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.organization_repository import OrganizationRepository
from app.infrastructure.database.repositories.user_repository import UserRepository
from app.infrastructure.database.session import get_db
from app.infrastructure.redis import keys as redis_keys
from app.infrastructure.redis.client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/sso", tags=["Auth"])

_OAUTH_STATE_TTL = 600


def _frontend_redirect(path: str = "/auth/login", **params: str) -> str:
    base = settings.FRONTEND_URL.rstrip("/")
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v})
    return f"{base}{path}" + (f"?{qs}" if qs else "")


async def _org_by_slug(db: AsyncSession, slug: str) -> Organization:
    org = await OrganizationRepository(db).get_by_slug(slug.strip().lower())
    if org is None:
        raise not_found("Organization not found")
    return org


async def _sso_config(db: AsyncSession, org_id: UUID) -> SsoConfiguration:
    row = (
        await db.execute(select(SsoConfiguration).where(SsoConfiguration.org_id == org_id))
    ).scalar_one_or_none()
    if row is None or not row.is_active:
        raise not_found("SSO is not configured for this organization")
    return row


def _email_allowed(email: str, domains: list[str]) -> bool:
    if not domains:
        return True
    dom = email.split("@")[-1].lower() if "@" in email else ""
    return dom in {d.lower() for d in domains}


async def _resolve_sso_user(
    db: AsyncSession,
    *,
    org: Organization,
    cfg: SsoConfiguration,
    email: str,
    full_name: str,
    subject: str,
    provider: str,
) -> User:
    if not _email_allowed(email, list(cfg.allowed_email_domains or [])):
        raise bad_request("EMAIL_NOT_ALLOWED", "Email domain is not allowed for this organization")

    repo = UserRepository(db)
    user = await repo.get_by_email(email)
    if user and user.org_id != org.id:
        raise bad_request("EMAIL_TAKEN", "Email belongs to another organization")

    if user is None:
        if not cfg.auto_provision_users:
            raise bad_request("USER_NOT_PROVISIONED", "No account exists for this email")
        user = await repo.create(
            org_id=org.id,
            email=email,
            password_hash=None,
            role=cfg.default_role,
        )
        user.full_name = full_name
        user.is_verified = True
        user.auth_provider = provider
        user.auth_provider_id = subject
        user.sso_provider = provider
        user.sso_subject = subject
    else:
        user.auth_provider = provider
        user.auth_provider_id = subject
        user.sso_provider = provider
        user.sso_subject = subject
        if full_name and not user.full_name:
            user.full_name = full_name
        user.is_verified = True

    await db.flush()
    return user


@router.get("/{org_slug}/status")
async def sso_status(org_slug: str, db: AsyncSession = Depends(get_db)) -> dict:
    if settings.DEPLOYMENT_MODE != "self_hosted":
        return {"enabled": False}
    try:
        org = await _org_by_slug(db, org_slug)
        cfg = (
            await db.execute(select(SsoConfiguration).where(SsoConfiguration.org_id == org.id))
        ).scalar_one_or_none()
        if cfg is None or not cfg.is_active:
            return {"enabled": False, "provider": None}
        return {"enabled": True, "provider": cfg.provider}
    except Exception:
        return {"enabled": False}


@router.get("/{org_slug}/login")
async def sso_login_start(
    org_slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    if settings.DEPLOYMENT_MODE != "self_hosted":
        raise not_found()
    org = await _org_by_slug(db, org_slug)
    await require_feature(db, org.id, "sso")
    cfg = await _sso_config(db, org.id)

    state = secrets.token_urlsafe(32)
    r = await get_redis()
    payload = {"org_id": str(org.id), "slug": org_slug, "provider": cfg.provider}
    if r is not None:
        await r.set(redis_keys.sso_oidc_state(state), json.dumps(payload), ex=_OAUTH_STATE_TTL)

    api_base = str(request.base_url).rstrip("/")
    if cfg.provider == "oidc":
        if not cfg.discovery_url:
            raise bad_request("BAD_REQUEST", "OIDC discovery_url missing")
        async with httpx.AsyncClient(timeout=15.0) as client:
            disc = (await client.get(cfg.discovery_url)).json()
        auth_endpoint = disc.get("authorization_endpoint")
        if not auth_endpoint:
            raise bad_request("BAD_REQUEST", "Invalid OIDC discovery document")
        redirect_uri = f"{api_base}/api/v1/auth/sso/{org_slug}/callback"
        scopes = (cfg.scopes or "openid email profile").strip()
        params = {
            "client_id": cfg.client_id or "",
            "response_type": "code",
            "scope": scopes,
            "redirect_uri": redirect_uri,
            "state": state,
        }
        url = f"{auth_endpoint}?{urllib.parse.urlencode(params)}"
        return RedirectResponse(url, status_code=302)

    if cfg.provider == "saml":
        try:
            from onelogin.saml2.auth import OneLogin_Saml2_Auth  # type: ignore
        except ImportError:
            raise bad_request(
                "SAML_DEPENDENCY_MISSING",
                "python3-saml is not installed on the server",
            )

        try:
            saml_settings = _saml_settings_dict(cfg, org_slug, api_base)
            req = await _prepare_saml_request(request)
            auth = OneLogin_Saml2_Auth(req, saml_settings)
            return RedirectResponse(auth.login(state), status_code=302)
        except Exception:
            logger.exception("Failed to start SAML login for org %s", org.id)
            return RedirectResponse(_frontend_redirect(error="sso_misconfigured"))

    raise bad_request("BAD_REQUEST", "Unknown SSO provider")


@router.get("/{org_slug}/callback")
async def sso_oidc_callback(
    org_slug: str,
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    if settings.DEPLOYMENT_MODE != "self_hosted":
        raise not_found()
    org = await _org_by_slug(db, org_slug)
    await require_feature(db, org.id, "sso")
    cfg = await _sso_config(db, org.id)
    if cfg.provider != "oidc":
        raise bad_request("BAD_REQUEST", "Not an OIDC configuration")

    r = await get_redis()
    if r is None:
        return RedirectResponse(_frontend_redirect(error="sso_unavailable"))
    raw = await r.get(redis_keys.sso_oidc_state(state))
    if not raw:
        return RedirectResponse(_frontend_redirect(error="invalid_state"))
    await r.delete(redis_keys.sso_oidc_state(state))

    api_base = str(request.base_url).rstrip("/")
    redirect_uri = f"{api_base}/api/v1/auth/sso/{org_slug}/callback"
    client_secret = decrypt_secret(cfg.client_secret_encrypted) if cfg.client_secret_encrypted else ""

    if not cfg.discovery_url:
        return RedirectResponse(_frontend_redirect(error="sso_misconfigured"))

    claims: dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            disc_resp = await client.get(cfg.discovery_url)
            disc_resp.raise_for_status()
            disc = disc_resp.json()
            token_url = disc.get("token_endpoint")
            if not token_url:
                return RedirectResponse(_frontend_redirect(error="sso_misconfigured"))
            token_resp = await client.post(
                token_url,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": cfg.client_id or "",
                    "client_secret": client_secret,
                },
                headers={"Accept": "application/json"},
            )
            token_resp.raise_for_status()
            tokens = token_resp.json()
            id_token = tokens.get("id_token")
            if id_token:
                try:
                    claims = jwt.decode(id_token, options={"verify_signature": False})
                except jwt.PyJWTError:
                    claims = {}
            if not claims and tokens.get("access_token") and disc.get("userinfo_endpoint"):
                ui = await client.get(
                    disc["userinfo_endpoint"],
                    headers={"Authorization": f"Bearer {tokens['access_token']}"},
                )
                ui.raise_for_status()
                claims = ui.json()
    except httpx.HTTPError:
        logger.exception("OIDC token/userinfo exchange failed for org %s", org.id)
        return RedirectResponse(_frontend_redirect(error="sso_exchange_failed"))

    email = str(claims.get(cfg.email_claim) or claims.get("email") or "").strip().lower()
    if not email:
        return RedirectResponse(_frontend_redirect(error="email_missing"))
    name = str(claims.get(cfg.name_claim) or claims.get("name") or email.split("@")[0])
    subject = str(claims.get("sub") or email)

    try:
        user = await _resolve_sso_user(
            db,
            org=org,
            cfg=cfg,
            email=email,
            full_name=name,
            subject=subject,
            provider="oidc",
        )
    except Exception:
        logger.exception("SSO user resolution failed for %s in org %s", email, org.id)
        return RedirectResponse(_frontend_redirect(error="sso_user_resolution_failed"))

    user.last_login_at = datetime.now(timezone.utc)
    access, refresh = await issue_tokens(user, org.id)
    await db.commit()
    return RedirectResponse(
        _frontend_redirect(
            "/auth/sso/callback",
            access_token=access,
            refresh_token=refresh,
        ),
        status_code=302,
    )


@router.post("/{org_slug}/saml/acs")
async def sso_saml_acs(
    org_slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    if settings.DEPLOYMENT_MODE != "self_hosted":
        raise not_found()
    org = await _org_by_slug(db, org_slug)
    await require_feature(db, org.id, "sso")
    cfg = await _sso_config(db, org.id)
    if cfg.provider != "saml":
        raise bad_request("BAD_REQUEST", "Not a SAML configuration")

    try:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth  # type: ignore
    except ImportError:
        raise bad_request(
            "SAML_DEPENDENCY_MISSING",
            "python3-saml is not installed on the server",
        )

    api_base = str(request.base_url).rstrip("/")
    try:
        saml_settings = _saml_settings_dict(cfg, org_slug, api_base)
        req = await _prepare_saml_request(request)
        auth = OneLogin_Saml2_Auth(req, saml_settings)
        auth.process_response()
    except Exception:
        logger.exception("SAML response processing failed for org %s", org.id)
        return RedirectResponse(_frontend_redirect(error="saml_failed"))

    if auth.get_errors():
        logger.warning("SAML errors: %s", auth.get_errors())
        return RedirectResponse(_frontend_redirect(error="saml_failed"))

    attrs = auth.get_attributes()
    email = _saml_attr(attrs, cfg.email_claim) or _saml_attr(attrs, "email")
    if not email:
        return RedirectResponse(_frontend_redirect(error="email_missing"))
    email = email.strip().lower()
    name = _saml_attr(attrs, cfg.name_claim) or _saml_attr(attrs, "displayName") or email.split("@")[0]
    subject = auth.get_nameid() or email

    try:
        user = await _resolve_sso_user(
            db,
            org=org,
            cfg=cfg,
            email=email,
            full_name=name,
            subject=subject,
            provider="saml",
        )
    except Exception:
        logger.exception("SSO user resolution failed for %s in org %s", email, org.id)
        return RedirectResponse(_frontend_redirect(error="sso_user_resolution_failed"))

    user.last_login_at = datetime.now(timezone.utc)
    access, refresh = await issue_tokens(user, org.id)
    await db.commit()
    return RedirectResponse(
        _frontend_redirect(
            "/auth/sso/callback",
            access_token=access,
            refresh_token=refresh,
        ),
        status_code=302,
    )


def _saml_attr(attrs: dict, key: str) -> str | None:
    val = attrs.get(key)
    if isinstance(val, list) and val:
        return str(val[0])
    if val:
        return str(val)
    return None


async def _prepare_saml_request(request: Request) -> dict:
    post_data: dict[str, str] = {}
    if request.method == "POST":
        form = await request.form()
        post_data = {k: str(v) for k, v in form.items()}
    return {
        "https": "on" if request.url.scheme == "https" else "off",
        "http_host": request.headers.get("host", "localhost"),
        "script_name": request.url.path,
        "server_port": request.url.port or (443 if request.url.scheme == "https" else 80),
        "get_data": dict(request.query_params),
        "post_data": post_data,
    }


def _saml_settings_dict(cfg: SsoConfiguration, org_slug: str, api_base: str) -> dict:
    try:
        from onelogin.saml2.idp_metadata_parser import (  # type: ignore
            OneLogin_Saml2_IdPMetadataParser,
        )
    except ImportError as exc:
        raise RuntimeError("python3-saml is not installed") from exc

    acs = f"{api_base}/api/v1/auth/sso/{org_slug}/saml/acs"
    settings_dict: dict[str, Any] = {
        "strict": True,
        "debug": False,
        "sp": {
            "entityId": cfg.entity_id or f"{api_base}/api/v1/auth/sso/{org_slug}",
            "assertionConsumerService": {
                "url": acs,
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
        },
    }
    if cfg.metadata_xml:
        parsed = OneLogin_Saml2_IdPMetadataParser.parse(cfg.metadata_xml)
        settings_dict.update(parsed)
    elif cfg.metadata_url:
        parsed = OneLogin_Saml2_IdPMetadataParser.parse_remote(cfg.metadata_url)
        settings_dict.update(parsed)
    return settings_dict
