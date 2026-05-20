from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models.studio_query_run import StudioQueryRun


class StudioQueryRunRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id_and_org(self, run_id: UUID, org_id: UUID) -> StudioQueryRun | None:
        result = await self.db.execute(
            select(StudioQueryRun).where(
                StudioQueryRun.id == run_id,
                StudioQueryRun.org_id == org_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, run_id: UUID) -> StudioQueryRun | None:
        return await self.db.get(StudioQueryRun, run_id)

    async def list_by_query(
        self, query_id: UUID, org_id: UUID, *, limit: int = 20
    ) -> list[StudioQueryRun]:
        result = await self.db.execute(
            select(StudioQueryRun)
            .where(
                StudioQueryRun.query_id == query_id,
                StudioQueryRun.org_id == org_id,
            )
            .order_by(StudioQueryRun.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def create(
        self,
        org_id: UUID,
        query_id: UUID | None,
        triggered_by: UUID | None,
        param_values: dict,
    ) -> StudioQueryRun:
        run = StudioQueryRun(
            org_id=org_id,
            query_id=query_id,
            triggered_by=triggered_by,
            param_values=param_values,
            status="pending",
        )
        self.db.add(run)
        await self.db.flush()
        return run

    async def mark_running(self, run: StudioQueryRun) -> None:
        run.status = "running"
        run.started_at = datetime.now(timezone.utc)
        await self.db.flush()

    async def mark_completed(self, run: StudioQueryRun, row_count: int) -> None:
        run.status = "completed"
        run.row_count = row_count
        run.completed_at = datetime.now(timezone.utc)
        await self.db.flush()

    async def mark_failed(self, run: StudioQueryRun, error: str) -> None:
        run.status = "failed"
        run.error = error[:2000]
        run.completed_at = datetime.now(timezone.utc)
        await self.db.flush()
