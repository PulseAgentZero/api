"""Fixed-window rate limits (Redis INCR + EXPIRE). No-op when Redis is unavailable."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from starlette.requests import Request

from app.api.errors import rate_limited
from app.infrastructure.audit import request_audit_context

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

# Per-IP limits (requests per 60s window) — aligned with M11 auth hardening targets.
LOGIN_IP_PER_MIN = 10
SIGNUP_IP_PER_MIN = 5
FORGOT_PASSWORD_IP_PER_MIN = 5
RESET_PASSWORD_IP_PER_MIN = 10
VERIFY_EMAIL_IP_PER_MIN = 20
REFRESH_IP_PER_MIN = 30
ACCEPT_INVITE_IP_PER_MIN = 10
OAUTH_START_IP_PER_MIN = 20

# Per-email limits (longer windows for outbound email abuse).
FORGOT_PASSWORD_EMAIL_PER_HOUR = 3
SIGNUP_EMAIL_PER_HOUR = 3

# Org invite email volume.
INVITE_ORG_PER_HOUR = 30
INVITE_RESEND_COOLDOWN_SEC = 60


def client_ip(request: Request) -> str:
    ip, _ = request_audit_context(request)
    return ip or "unknown"


async def enforce_fixed_window_limit(
    redis: "Redis | None",
    *,
    key: str,
    limit: int,
    window_sec: int = 60,
    message: str | None = None,
) -> None:
    """Increment ``key`` and raise ``RATE_LIMITED`` (429) when count exceeds ``limit``."""
    if redis is None:
        return
    try:
        n = await redis.incr(key)
        if n == 1:
            await redis.expire(key, window_sec)
        if n > limit:
            raise rate_limited(
                message or "Too many requests. Please try again later."
            )
    except Exception as exc:
        if hasattr(exc, "status_code"):
            raise
        logger.warning("Rate limit Redis error for key %s: %s", key, exc)


async def enforce_auth_ip_limit(
    redis: "Redis | None",
    request: Request,
    action: str,
    *,
    limit: int,
    window_sec: int = 60,
    message: str | None = None,
) -> None:
    from app.infrastructure.redis.keys import auth_rl_ip

    await enforce_fixed_window_limit(
        redis,
        key=auth_rl_ip(client_ip(request), action),
        limit=limit,
        window_sec=window_sec,
        message=message,
    )


async def enforce_auth_email_limit(
    redis: "Redis | None",
    email: str,
    action: str,
    *,
    limit: int,
    window_sec: int,
    message: str | None = None,
) -> None:
    from app.infrastructure.redis.keys import auth_rl_email

    await enforce_fixed_window_limit(
        redis,
        key=auth_rl_email(email, action),
        limit=limit,
        window_sec=window_sec,
        message=message,
    )
