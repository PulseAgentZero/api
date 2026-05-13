from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models.api_key import ApiKey


class ApiKeyRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_hash(self, key_hash: str) -> ApiKey | None:
        r = await self.db.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))
        return r.scalar_one_or_none()

    async def touch_last_used(self, api_key_id: UUID) -> None:
        row = await self.db.get(ApiKey, api_key_id)
        if row is None:
            return
        row.last_used_at = datetime.now(timezone.utc)
        await self.db.flush()
