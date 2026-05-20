from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.base import touch_updated_at
from app.infrastructure.database.models.connection import Connection


class ConnectionRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, connection_id: UUID) -> Connection | None:
        return await self.db.get(Connection, connection_id)

    async def list_by_org(self, org_id: UUID, *, include_deleted: bool = False) -> list[Connection]:
        stmt = select(Connection).where(Connection.org_id == org_id)
        if not include_deleted:
            stmt = stmt.where(Connection.deleted_at.is_(None))
        stmt = stmt.order_by(Connection.created_at)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def count_active(self, org_id: UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(Connection)
            .where(
                Connection.org_id == org_id,
                Connection.deleted_at.is_(None),
            )
        )
        return int(await self.db.scalar(stmt) or 0)

    async def create(
        self,
        org_id: UUID,
        *,
        encrypted_dsn: str | None = None,
        name: str | None = None,
        connector_type: str | None = None,
        connection_meta: dict | None = None,
    ) -> Connection:
        conn = Connection(
            org_id=org_id,
            encrypted_dsn=encrypted_dsn,
            name=name or "My Connection",
            connector_type=connector_type or "postgresql",
            connection_meta=connection_meta or {},
        )
        self.db.add(conn)
        await self.db.flush()
        return conn

    async def update(self, connection_id: UUID, **fields) -> Connection | None:
        conn = await self.get_by_id(connection_id)
        if conn is None:
            return None
        for key, value in fields.items():
            if hasattr(conn, key):
                setattr(conn, key, value)
        touch_updated_at(conn)
        await self.db.flush()
        return conn

    async def soft_delete(self, connection_id: UUID) -> bool:
        conn = await self.get_by_id(connection_id)
        if conn is None or conn.deleted_at is not None:
            return False
        conn.deleted_at = datetime.now(timezone.utc)
        touch_updated_at(conn)
        await self.db.flush()
        return True

    async def delete(self, connection_id: UUID) -> bool:
        """Hard delete (legacy); prefer soft_delete."""
        conn = await self.get_by_id(connection_id)
        if conn is None:
            return False
        await self.db.delete(conn)
        await self.db.flush()
        return True
