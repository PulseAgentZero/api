from uuid import UUID

from sqlalchemy import select
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

    async def create(self, org_id: UUID, email: str, password_hash: str, role: str = "ops_manager") -> User:
        user = User(
            org_id=org_id,
            email=email,
            password_hash=password_hash,
            role=role,
        )
        self.db.add(user)
        await self.db.flush()
        return user
