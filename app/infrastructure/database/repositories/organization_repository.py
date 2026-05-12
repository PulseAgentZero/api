from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models.organization import Organization


class OrganizationRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, org_id: UUID) -> Organization | None:
        return await self.db.get(Organization, org_id)

    async def create(self, name: str) -> Organization:
        org = Organization(name=name)
        self.db.add(org)
        await self.db.flush()
        return org
