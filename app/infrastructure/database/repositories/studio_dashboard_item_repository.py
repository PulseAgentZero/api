from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models.studio_dashboard_item import StudioDashboardItem


class StudioDashboardItemRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_by_dashboard(
        self, dashboard_id: UUID, org_id: UUID
    ) -> list[StudioDashboardItem]:
        result = await self.db.execute(
            select(StudioDashboardItem)
            .where(
                StudioDashboardItem.dashboard_id == dashboard_id,
                StudioDashboardItem.org_id == org_id,
            )
            .order_by(StudioDashboardItem.position.asc())
        )
        return list(result.scalars().all())

    async def count_by_dashboard(self, dashboard_id: UUID) -> int:
        return int(
            await self.db.scalar(
                select(func.count())
                .select_from(StudioDashboardItem)
                .where(StudioDashboardItem.dashboard_id == dashboard_id)
            )
            or 0
        )

    async def get_by_id_and_org(
        self, item_id: UUID, org_id: UUID
    ) -> StudioDashboardItem | None:
        result = await self.db.execute(
            select(StudioDashboardItem).where(
                StudioDashboardItem.id == item_id,
                StudioDashboardItem.org_id == org_id,
            )
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        org_id: UUID,
        dashboard_id: UUID,
        visualization_id: UUID | None,
        position: int,
        *,
        panel_type: str = "visualization",
        content: str | None = None,
    ) -> StudioDashboardItem:
        item = StudioDashboardItem(
            org_id=org_id,
            dashboard_id=dashboard_id,
            visualization_id=visualization_id,
            position=position,
            panel_type=panel_type,
            content=content,
        )
        self.db.add(item)
        await self.db.flush()
        return item

    async def delete_by_dashboard(self, dashboard_id: UUID) -> None:
        await self.db.execute(
            delete(StudioDashboardItem).where(
                StudioDashboardItem.dashboard_id == dashboard_id
            )
        )
        await self.db.flush()

    async def delete(self, item: StudioDashboardItem) -> None:
        await self.db.delete(item)
        await self.db.flush()
