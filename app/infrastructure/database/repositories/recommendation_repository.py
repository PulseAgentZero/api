from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.base import touch_updated_at
from app.infrastructure.database.models.recommendation import Recommendation


class RecommendationRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, recommendation_id: UUID) -> Recommendation | None:
        return await self.db.get(Recommendation, recommendation_id)

    async def list_by_org(
        self,
        org_id: UUID,
        urgency: str | None = None,
        status: str | None = None,
        entity_id: str | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Recommendation]:
        stmt = select(Recommendation).where(Recommendation.org_id == org_id)
        if urgency:
            stmt = stmt.where(Recommendation.urgency == urgency)
        if status:
            stmt = stmt.where(Recommendation.status == status)
        if entity_id:
            stmt = stmt.where(Recommendation.entity_id == entity_id)
        stmt = stmt.order_by(Recommendation.created_at.desc()).offset(offset).limit(limit)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def count_by_org(
        self,
        org_id: UUID,
        urgency: str | None = None,
        status: str | None = None,
        entity_id: str | None = None,
    ) -> int:
        stmt = select(func.count()).select_from(Recommendation).where(Recommendation.org_id == org_id)
        if urgency:
            stmt = stmt.where(Recommendation.urgency == urgency)
        if status:
            stmt = stmt.where(Recommendation.status == status)
        if entity_id:
            stmt = stmt.where(Recommendation.entity_id == entity_id)
        return int(await self.db.scalar(stmt) or 0)

    async def create(self, **fields) -> Recommendation:
        rec = Recommendation(**fields)
        self.db.add(rec)
        await self.db.flush()
        return rec

    async def update(self, recommendation_id: UUID, **fields) -> Recommendation | None:
        rec = await self.get_by_id(recommendation_id)
        if not rec:
            return None
        for key, value in fields.items():
            setattr(rec, key, value)
        touch_updated_at(rec)
        return rec

    async def delete(self, recommendation_id: UUID) -> bool:
        rec = await self.get_by_id(recommendation_id)
        if not rec:
            return False
        await self.db.delete(rec)
        await self.db.flush()
        return True

    async def delete_by_org(self, org_id: UUID) -> int:
        recs = await self.list_by_org(org_id)
        for rec in recs:
            await self.db.delete(rec)
        await self.db.flush()
        return len(recs)
