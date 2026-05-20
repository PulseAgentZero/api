"""Redis-backed MFA challenge and setup tokens."""

from __future__ import annotations

import json
import secrets
from typing import Any
from uuid import UUID

from app.infrastructure.redis import keys
from app.infrastructure.redis.client import get_redis

MFA_LOGIN_TTL_SEC = 5 * 60
MFA_SETUP_TTL_SEC = 15 * 60
ORG_DELETE_CODE_TTL_SEC = 10 * 60


async def _require_redis():
    r = await get_redis()
    if r is None:
        from app.api.errors import service_unavailable

        raise service_unavailable(
            "REDIS_REQUIRED",
            "This action requires Redis. Please try again later.",
        )
    return r


async def create_mfa_login_token(*, user_id: UUID, org_id: UUID) -> str:
    r = await _require_redis()
    token = secrets.token_urlsafe(32)
    payload = json.dumps({"user_id": str(user_id), "org_id": str(org_id)})
    await r.set(keys.mfa_login(token), payload, ex=MFA_LOGIN_TTL_SEC)
    return token


async def get_mfa_login_token(token: str) -> dict[str, Any] | None:
    r = await get_redis()
    if r is None:
        return None
    raw = await r.get(keys.mfa_login(token))
    if not raw:
        return None
    text = raw.decode() if isinstance(raw, bytes) else str(raw)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


async def delete_mfa_login_token(token: str) -> None:
    r = await get_redis()
    if r is None:
        return
    await r.delete(keys.mfa_login(token))


async def create_mfa_setup_token(*, user_id: UUID) -> str:
    r = await _require_redis()
    token = secrets.token_urlsafe(32)
    await r.set(keys.mfa_setup(token), str(user_id), ex=MFA_SETUP_TTL_SEC)
    return token


async def get_mfa_setup_user_id(token: str) -> str | None:
    r = await get_redis()
    if r is None:
        return None
    raw = await r.get(keys.mfa_setup(token))
    if not raw:
        return None
    return raw.decode() if isinstance(raw, bytes) else str(raw)


async def delete_mfa_setup_token(token: str) -> None:
    r = await get_redis()
    if r is None:
        return
    await r.delete(keys.mfa_setup(token))


async def set_org_delete_code(*, org_id: UUID, owner_id: UUID, code: str) -> None:
    r = await _require_redis()
    await r.set(
        keys.org_delete_code(str(org_id), str(owner_id)),
        code,
        ex=ORG_DELETE_CODE_TTL_SEC,
    )


async def get_org_delete_code(*, org_id: UUID, owner_id: UUID) -> str | None:
    r = await get_redis()
    if r is None:
        return None
    raw = await r.get(keys.org_delete_code(str(org_id), str(owner_id)))
    if not raw:
        return None
    return raw.decode() if isinstance(raw, bytes) else str(raw)


async def delete_org_delete_code(*, org_id: UUID, owner_id: UUID) -> None:
    r = await get_redis()
    if r is None:
        return
    await r.delete(keys.org_delete_code(str(org_id), str(owner_id)))
