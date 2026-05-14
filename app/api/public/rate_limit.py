"""Per–API-key rate limits for the public API (fixed window, Redis-backed)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.api.errors import rate_limited

if TYPE_CHECKING:
    from redis.asyncio import Redis

# Matches OpenAPI copy on public_app (read vs write key scope).
_READ_KEY_CAP_PER_MIN = 30
_WRITE_KEY_CAP_PER_MIN = 10


async def enforce_public_api_rate_limit(
    redis: "Redis | None",
    *,
    scope: str,
    api_key_id: str,
) -> None:
    """Raise ``RATE_LIMITED`` when the key exceeds its per-minute quota."""
    if redis is None:
        return
    cap = _WRITE_KEY_CAP_PER_MIN if scope == "write" else _READ_KEY_CAP_PER_MIN
    key = f"public_api_rl:{api_key_id}"
    n = await redis.incr(key)
    if n == 1:
        await redis.expire(key, 60)
    if n > cap:
        raise rate_limited(
            f"Rate limit exceeded ({cap} requests per minute for this API key). "
            "See API documentation for limits."
        )
