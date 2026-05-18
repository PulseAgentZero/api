from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models.studio_query import StudioQuery


class StudioQueryRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id_and_org(self, query_id: UUID, org_id: UUID) -> StudioQuery | None:
        result = await self.db.execute(
            select(StudioQuery).where(
                StudioQuery.id == query_id,
                StudioQuery.org_id == org_id,
            )
        )
        return result.scalar_one_or_none()

    async def search(
        self,
        org_id: UUID,
        *,
        q: str | None = None,
        tags: list[str] | None = None,
        starred_ids: set[UUID] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[StudioQuery]:
        stmt = select(StudioQuery).where(StudioQuery.org_id == org_id)
        if q:
            stmt = stmt.where(
                or_(
                    StudioQuery.name.ilike(f"%{q}%"),
                    StudioQuery.description.ilike(f"%{q}%"),
                )
            )
        if tags:
            stmt = stmt.where(
                StudioQuery.tags.op("@>")(cast(json.dumps(tags), JSONB))
            )
        if starred_ids is not None:
            if not starred_ids:
                return []
            stmt = stmt.where(StudioQuery.id.in_(starred_ids))
        stmt = stmt.order_by(StudioQuery.updated_at.desc()).offset(offset).limit(limit)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def search_count(
        self,
        org_id: UUID,
        *,
        q: str | None = None,
        tags: list[str] | None = None,
        starred_ids: set[UUID] | None = None,
    ) -> int:
        stmt = select(func.count()).select_from(StudioQuery).where(StudioQuery.org_id == org_id)
        if q:
            stmt = stmt.where(
                or_(
                    StudioQuery.name.ilike(f"%{q}%"),
                    StudioQuery.description.ilike(f"%{q}%"),
                )
            )
        if tags:
            stmt = stmt.where(
                StudioQuery.tags.op("@>")(cast(json.dumps(tags), JSONB))
            )
        if starred_ids is not None:
            if not starred_ids:
                return 0
            stmt = stmt.where(StudioQuery.id.in_(starred_ids))
        return int(await self.db.scalar(stmt) or 0)

    async def list_by_org(
        self, org_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> list[StudioQuery]:
        result = await self.db.execute(
            select(StudioQuery)
            .where(StudioQuery.org_id == org_id)
            .order_by(StudioQuery.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def count_by_org(self, org_id: UUID) -> int:
        return int(
            await self.db.scalar(
                select(func.count()).select_from(StudioQuery).where(StudioQuery.org_id == org_id)
            )
            or 0
        )

    async def create(
        self,
        org_id: UUID,
        created_by: UUID,
        *,
        name: str,
        description: str | None,
        sql_text: str,
        connection_id: UUID | None,
        params: list | None = None,
        refresh_cron: str | None = None,
        refresh_enabled: bool = False,
        tags: list | None = None,
    ) -> StudioQuery:
        q = StudioQuery(
            org_id=org_id,
            created_by=created_by,
            name=name,
            description=description,
            sql_text=sql_text,
            connection_id=connection_id,
            params=params or [],
            refresh_cron=refresh_cron,
            refresh_enabled=refresh_enabled,
            tags=tags or [],
        )
        self.db.add(q)
        await self.db.flush()
        return q

    async def update(self, query: StudioQuery, **fields) -> StudioQuery:
        for key, value in fields.items():
            setattr(query, key, value)
        await self.db.flush()
        return query

    async def delete(self, query: StudioQuery) -> None:
        await self.db.delete(query)
        await self.db.flush()

    async def touch_last_run(self, query: StudioQuery, row_count: int) -> None:
        query.last_run_at = datetime.now(timezone.utc)
        query.last_run_row_count = row_count
        await self.db.flush()
