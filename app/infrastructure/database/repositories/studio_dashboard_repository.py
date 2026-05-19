from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.base import touch_updated_at
from app.infrastructure.database.models.studio_dashboard import StudioDashboard


class StudioDashboardRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id_and_org(
        self, dashboard_id: UUID, org_id: UUID
    ) -> StudioDashboard | None:
        result = await self.db.execute(
            select(StudioDashboard).where(
                StudioDashboard.id == dashboard_id,
                StudioDashboard.org_id == org_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_slug(self, slug: str) -> StudioDashboard | None:
        """Return a public dashboard by slug. Only returns is_public=True dashboards."""
        result = await self.db.execute(
            select(StudioDashboard).where(
                StudioDashboard.slug == slug,
                StudioDashboard.is_public.is_(True),
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
    ) -> list[StudioDashboard]:
        stmt = select(StudioDashboard).where(StudioDashboard.org_id == org_id)
        if q:
            stmt = stmt.where(
                or_(
                    StudioDashboard.name.ilike(f"%{q}%"),
                    StudioDashboard.description.ilike(f"%{q}%"),
                )
            )
        if tags:
            stmt = stmt.where(
                StudioDashboard.tags.op("@>")(cast(json.dumps(tags), JSONB))
            )
        if starred_ids is not None:
            if not starred_ids:
                return []
            stmt = stmt.where(StudioDashboard.id.in_(starred_ids))
        stmt = stmt.order_by(StudioDashboard.updated_at.desc()).offset(offset).limit(limit)
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
        stmt = (
            select(func.count())
            .select_from(StudioDashboard)
            .where(StudioDashboard.org_id == org_id)
        )
        if q:
            stmt = stmt.where(
                or_(
                    StudioDashboard.name.ilike(f"%{q}%"),
                    StudioDashboard.description.ilike(f"%{q}%"),
                )
            )
        if tags:
            stmt = stmt.where(
                StudioDashboard.tags.op("@>")(cast(json.dumps(tags), JSONB))
            )
        if starred_ids is not None:
            if not starred_ids:
                return 0
            stmt = stmt.where(StudioDashboard.id.in_(starred_ids))
        return int(await self.db.scalar(stmt) or 0)

    async def list_by_org(
        self, org_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> list[StudioDashboard]:
        result = await self.db.execute(
            select(StudioDashboard)
            .where(StudioDashboard.org_id == org_id)
            .order_by(StudioDashboard.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def count_by_org(self, org_id: UUID) -> int:
        return int(
            await self.db.scalar(
                select(func.count())
                .select_from(StudioDashboard)
                .where(StudioDashboard.org_id == org_id)
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
        is_public: bool,
        slug: str | None,
        layout: list,
        dashboard_params: list | None = None,
        tags: list | None = None,
    ) -> StudioDashboard:
        dashboard = StudioDashboard(
            org_id=org_id,
            created_by=created_by,
            name=name,
            description=description,
            is_public=is_public,
            slug=slug,
            layout=layout,
            dashboard_params=dashboard_params or [],
            tags=tags or [],
        )
        self.db.add(dashboard)
        await self.db.flush()
        return dashboard

    async def update(self, dashboard: StudioDashboard, **fields) -> StudioDashboard:
        for key, value in fields.items():
            setattr(dashboard, key, value)
        touch_updated_at(dashboard)
        await self.db.flush()
        return dashboard

    async def delete(self, dashboard: StudioDashboard) -> None:
        await self.db.delete(dashboard)
        await self.db.flush()

    async def slug_exists(self, slug: str) -> bool:
        result = await self.db.scalar(
            select(func.count())
            .select_from(StudioDashboard)
            .where(StudioDashboard.slug == slug)
        )
        return (result or 0) > 0
