from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import forbidden, unauthorized
from app.api.public.rate_limit import enforce_public_api_rate_limit
from app.infrastructure.database.session import get_db
from app.infrastructure.redis.client import get_redis


@dataclass
class ApiKeyContext:
    org_id: str
    api_key_id: str
    scope: str          # read | write


def require_api_key(required_scope: str = "read"):
    """
    FastAPI dependency for public API routes.
    Validates X-API-Key header. Rejects JWT tokens entirely.

    Usage:
        @router.get("/entities")
        async def list_entities(ctx=Depends(require_api_key("read"))):
            ...org_id = ctx.org_id
    """
    async def _check(
        x_api_key: str = Header(..., alias="X-API-Key"),
        db: AsyncSession = Depends(get_db),
    ) -> ApiKeyContext:
        from app.infrastructure.database.repositories.api_key_repository import ApiKeyRepository

        key_hash = sha256(x_api_key.encode()).hexdigest()
        repo = ApiKeyRepository(db)
        api_key = await repo.get_by_hash(key_hash)

        if not api_key or api_key.revoked_at is not None:
            raise unauthorized("INVALID_API_KEY", "Invalid or revoked API key")

        if api_key.expires_at and api_key.expires_at < datetime.now(timezone.utc):
            raise unauthorized("API_KEY_EXPIRED", "API key has expired")

        if required_scope == "write" and api_key.scope != "write":
            raise forbidden(
                "INSUFFICIENT_SCOPE",
                "This action requires a write-scoped API key",
            )

        r = await get_redis()
        await enforce_public_api_rate_limit(r, scope=api_key.scope, api_key_id=str(api_key.id))

        await repo.touch_last_used(api_key.id)

        return ApiKeyContext(
            org_id=str(api_key.org_id),
            api_key_id=str(api_key.id),
            scope=api_key.scope,
        )

    return _check
