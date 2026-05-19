from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.base import touch_updated_at
from app.infrastructure.database.models.studio_visualization import StudioVisualization


class StudioVisualizationRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id_and_org(self, viz_id: UUID, org_id: UUID) -> StudioVisualization | None:
        result = await self.db.execute(
            select(StudioVisualization).where(
                StudioVisualization.id == viz_id,
                StudioVisualization.org_id == org_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_by_query(self, query_id: UUID, org_id: UUID) -> list[StudioVisualization]:
        result = await self.db.execute(
            select(StudioVisualization)
            .where(
                StudioVisualization.query_id == query_id,
                StudioVisualization.org_id == org_id,
            )
            .order_by(StudioVisualization.created_at.asc())
        )
        return list(result.scalars().all())

    async def list_by_org(
        self,
        org_id: UUID,
        *,
        limit: int = 100,
        offset: int = 0,
        query_id: UUID | None = None,
    ) -> list[StudioVisualization]:
        stmt = (
            select(StudioVisualization)
            .where(StudioVisualization.org_id == org_id)
            .order_by(StudioVisualization.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        if query_id is not None:
            stmt = stmt.where(StudioVisualization.query_id == query_id)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def list_by_ids(self, org_id: UUID, ids: list[UUID]) -> list[StudioVisualization]:
        if not ids:
            return []
        result = await self.db.execute(
            select(StudioVisualization).where(
                StudioVisualization.org_id == org_id,
                StudioVisualization.id.in_(ids),
            )
        )
        return list(result.scalars().all())

    async def create(
        self,
        org_id: UUID,
        query_id: UUID,
        created_by: UUID,
        *,
        name: str,
        chart_type: str,
        config: dict,
        column_formats: dict | None = None,
    ) -> StudioVisualization:
        viz = StudioVisualization(
            org_id=org_id,
            query_id=query_id,
            created_by=created_by,
            name=name,
            chart_type=chart_type,
            config=config,
            column_formats=column_formats or {},
        )
        self.db.add(viz)
        await self.db.flush()
        return viz

    async def update(self, viz: StudioVisualization, **fields) -> StudioVisualization:
        for key, value in fields.items():
            setattr(viz, key, value)
        touch_updated_at(viz)
        await self.db.flush()
        return viz

    async def delete(self, viz: StudioVisualization) -> None:
        await self.db.delete(viz)
        await self.db.flush()
