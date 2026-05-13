"""Redis-backed opaque tokens for auth and pipeline coordination."""

from __future__ import annotations

import json
import secrets
from typing import Any
from uuid import UUID

from app.infrastructure.redis import keys
from app.infrastructure.redis.client import get_redis

REFRESH_TTL_SEC = 7 * 24 * 3600
EMAIL_VERIFY_TTL_SEC = 24 * 3600
PW_RESET_TTL_SEC = 30 * 60


def _token_hex(nbytes: int = 32) -> str:
    return secrets.token_hex(nbytes)


async def set_refresh_token(user_id: UUID, org_id: UUID, role: str) -> str:
    r = await get_redis()
    if r is None:
        raise RuntimeError("REDIS_URL is required for refresh token storage")
    raw = _token_hex(32)
    key = keys.refresh(raw)
    payload = json.dumps({"user_id": str(user_id), "org_id": str(org_id), "role": role})
    await r.set(key, payload, ex=REFRESH_TTL_SEC)
    return raw


async def get_refresh_token(raw: str) -> dict[str, Any] | None:
    r = await get_redis()
    if r is None:
        return None
    key = keys.refresh(raw)
    data = await r.get(key)
    if not data:
        return None
    return json.loads(data)


async def delete_refresh_token(raw: str) -> None:
    r = await get_redis()
    if r is None:
        return
    await r.delete(keys.refresh(raw))


async def set_email_verify_token(user_id: UUID) -> str:
    r = await get_redis()
    if r is None:
        raise RuntimeError("REDIS_URL is required for email verification")
    token = _token_hex(24)
    await r.set(keys.email_verify(token), str(user_id), ex=EMAIL_VERIFY_TTL_SEC)
    return token


async def get_email_verify_token(token: str) -> str | None:
    r = await get_redis()
    if r is None:
        return None
    return await r.get(keys.email_verify(token))


async def delete_email_verify_token(token: str) -> None:
    r = await get_redis()
    if r is None:
        return
    await r.delete(keys.email_verify(token))


async def set_pw_reset_token(user_id: UUID) -> str:
    r = await get_redis()
    if r is None:
        raise RuntimeError("REDIS_URL is required for password reset")
    token = _token_hex(24)
    await r.set(keys.pw_reset(token), str(user_id), ex=PW_RESET_TTL_SEC)
    return token


async def get_pw_reset_token(token: str) -> str | None:
    r = await get_redis()
    if r is None:
        return None
    return await r.get(keys.pw_reset(token))


async def delete_pw_reset_token(token: str) -> None:
    r = await get_redis()
    if r is None:
        return
    await r.delete(keys.pw_reset(token))
