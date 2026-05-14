"""Async Redis singleton — all services import from here."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from app.config.settings import settings

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)
_redis: Optional["Redis"] = None


async def get_redis() -> Optional["Redis"]:
    """Return shared Redis client, or None if REDIS_URL is not configured."""
    global _redis
    if not settings.REDIS_URL:
        return None
    if _redis is None:
        from redis.asyncio import Redis as RedisCls

        _redis = RedisCls.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
        logger.info("Redis client initialised")
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
