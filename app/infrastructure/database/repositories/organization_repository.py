import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models.organization import Organization


def _slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return (s or "org")[:80]


class OrganizationRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, org_id: UUID) -> Organization | None:
        return await self.db.get(Organization, org_id)

    async def get_by_slug(self, slug: str) -> Organization | None:
        result = await self.db.execute(select(Organization).where(Organization.slug == slug))
        return result.scalar_one_or_none()

    async def create(self, name: str) -> Organization:
        base = _slugify(name)
        slug = base
        n = 0
        while True:
            existing = await self.db.execute(select(Organization.id).where(Organization.slug == slug))
            if existing.scalar_one_or_none() is None:
                break
            n += 1
            slug = f"{base}-{n}"[:80]

        org = Organization(name=name, slug=slug)
        self.db.add(org)
        await self.db.flush()
        return org
