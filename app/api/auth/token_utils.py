"""JWT access + refresh token issuance."""

from __future__ import annotations

from uuid import UUID

from app.api.auth.jwt_utils import create_access_token, create_refresh_token
from app.infrastructure.database.models.user import User
from app.infrastructure.redis import tokens as redis_tokens
from app.infrastructure.redis.client import get_redis


async def issue_tokens(user: User, org_id: UUID) -> tuple[str, str]:
    access = create_access_token(user.id, org_id, user.role, user.email)
    r = await get_redis()
    if r is not None:
        refresh = await redis_tokens.set_refresh_token(user.id, org_id, user.role)
    else:
        refresh = create_refresh_token(user.id)
    return access, refresh
