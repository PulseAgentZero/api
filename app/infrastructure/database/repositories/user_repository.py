from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models.user import User


class UserRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, user_id: UUID) -> User | None:
        return await self.db.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        result = await self.db.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def list_by_org(self, org_id: UUID) -> list[User]:
        result = await self.db.execute(
            select(User).where(User.org_id == org_id).order_by(User.created_at)
        )
        return list(result.scalars().all())

    async def count_admins(self, org_id: UUID) -> int:
        result = await self.db.execute(
            select(func.count(User.id)).where(User.org_id == org_id, User.role == "admin")
        )
        return int(result.scalar() or 0)

    async def create(
        self,
        org_id: UUID,
        email: str,
        password_hash: str | None,
        role: str = "analyst",
    ) -> User:
        user = User(
            org_id=org_id,
            email=email,
            password_hash=password_hash,
            role=role,
        )
        self.db.add(user)
        await self.db.flush()
        return user

    async def delete(self, user_id: UUID) -> bool:
        user = await self.get_by_id(user_id)
        if user is None:
            return False
        await self.db.delete(user)
        await self.db.flush()
        return True
