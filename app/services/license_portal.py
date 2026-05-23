"""License customer portal — magic-link sign-in for self-hosted buyers.

Lets people who purchased a self-hosted license retrieve their key(s) without
ever creating an Entivia Cloud workspace. The flow:

1. Buyer enters their email on ``/pricing/self-hosted/portal``.
2. We email a one-time magic link (15 min TTL, single use) keyed by Redis.
3. The link lands on a callback page that exchanges the token for a short-lived
   portal session JWT (also 15 min TTL).
4. The frontend calls the portal endpoints with that JWT in the Authorization
   header and shows the buyer's license keys.

Stateless after step 3 — the portal JWT is self-validating with the same
``settings.JWT_SECRET`` used for user auth. We tag it with ``typ="license_portal"``
so it can never be confused with a normal user access token.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

import jwt

from app.config.settings import settings
from app.infrastructure.redis.client import get_redis
from app.services.email_queue import queue_email

logger = logging.getLogger(__name__)

MAGIC_LINK_TTL_SEC = 15 * 60
PORTAL_SESSION_TTL_SEC = 15 * 60
PORTAL_TOKEN_TYPE = "license_portal"


class LicensePortalError(RuntimeError):
    """Portal infrastructure is unavailable (e.g. Redis missing)."""


class LicensePortalUnauthorized(RuntimeError):
    """Portal token is missing, malformed, or expired."""


def _magic_link_key(token: str) -> str:
    return f"selfhost_portal_magic:{token}"


def _token_hex(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


async def issue_magic_link_token(email: str) -> str:
    """Store a single-use magic-link token for ``email``. Returns the token."""
    r = await get_redis()
    if r is None:
        raise LicensePortalError(
            "Redis is required for the license customer portal but is not configured."
        )
    token = _token_hex(32)
    await r.set(_magic_link_key(token), email.strip().lower(), ex=MAGIC_LINK_TTL_SEC)
    return token


async def consume_magic_link_token(token: str) -> str | None:
    """Atomically read-and-delete a magic-link token. Returns email or None."""
    r = await get_redis()
    if r is None:
        return None
    key = _magic_link_key(token)
    raw = await r.get(key)
    if not raw:
        return None
    try:
        await r.delete(key)
    except Exception:  # pragma: no cover — Redis transient failure
        logger.warning("Failed to delete magic-link token after use; continuing")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return str(raw).strip().lower() or None


def create_portal_session_token(email: str) -> tuple[str, int]:
    """Issue a portal session JWT bound to ``email``. Returns (jwt, ttl_seconds)."""
    now = datetime.now(timezone.utc)
    payload = {
        "typ": PORTAL_TOKEN_TYPE,
        "sub": email.strip().lower(),
        "iat": now,
        "exp": now + timedelta(seconds=PORTAL_SESSION_TTL_SEC),
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, PORTAL_SESSION_TTL_SEC


def decode_portal_session_token(token: str) -> str:
    """Decode a portal session JWT. Raises ``LicensePortalUnauthorized`` on failure."""
    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
        )
    except jwt.ExpiredSignatureError as exc:
        raise LicensePortalUnauthorized("Portal session expired") from exc
    except jwt.InvalidTokenError as exc:
        raise LicensePortalUnauthorized("Invalid portal session") from exc

    if payload.get("typ") != PORTAL_TOKEN_TYPE:
        raise LicensePortalUnauthorized("Wrong token type for the license portal")
    email = payload.get("sub")
    if not isinstance(email, str) or not email:
        raise LicensePortalUnauthorized("Invalid portal session payload")
    return email.strip().lower()


async def resend_license_key_email(
    *, to: str, license_key: str, expires_at: str | None
) -> None:
    """Re-deliver a previously issued license key via the regular email queue."""
    await queue_email(
        "license_key",
        to=to,
        license_key=license_key,
        expires_at=expires_at,
    )
