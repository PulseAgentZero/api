"""Docker healthcheck for background workers that require Redis (worker, scheduler)."""

from __future__ import annotations

import asyncio
import sys


async def _main() -> int:
    from app.infrastructure.redis.client import get_redis

    r = await get_redis()
    if r is None:
        return 1
    await r.ping()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(_main()))
    except Exception:
        raise SystemExit(1)
