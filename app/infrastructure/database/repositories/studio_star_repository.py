from __future__ import annotations

import uuid
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models.studio_star import StudioStar


class StudioStarRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def upsert(
        self,
        user_id: UUID,
        org_id: UUID,
        resource_type: str,
        resource_id: UUID,
    ) -> None:
        stmt = (
            insert(StudioStar)
            .values(
                id=uuid.uuid4(),
                user_id=user_id,
                org_id=org_id,
                resource_type=resource_type,
                resource_id=resource_id,
            )
            .on_conflict_do_nothing(
                index_elements=["user_id", "resource_type", "resource_id"]
            )
        )
        await self.db.execute(stmt)
        await self.db.flush()

    async def delete(
        self,
        user_id: UUID,
        resource_type: str,
        resource_id: UUID,
    ) -> None:
        await self.db.execute(
            delete(StudioStar).where(
                StudioStar.user_id == user_id,
                StudioStar.resource_type == resource_type,
                StudioStar.resource_id == resource_id,
            )
        )
        await self.db.flush()

    async def get_starred_ids(
        self, user_id: UUID, resource_type: str
    ) -> set[UUID]:
        result = await self.db.execute(
            select(StudioStar.resource_id).where(
                StudioStar.user_id == user_id,
                StudioStar.resource_type == resource_type,
            )
        )
        return {row[0] for row in result.all()}
