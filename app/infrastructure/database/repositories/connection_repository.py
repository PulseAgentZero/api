from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models.connection import Connection


class ConnectionRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, connection_id: UUID) -> Connection | None:
        return await self.db.get(Connection, connection_id)

    async def list_by_org(self, org_id: UUID) -> list[Connection]:
        result = await self.db.execute(
            select(Connection).where(Connection.org_id == org_id).order_by(Connection.created_at)
        )
        return list(result.scalars().all())

    async def create(
        self,
        org_id: UUID,
        db_type: str,
        host: str,
        port: int,
        database_name: str,
        username: str,
        encrypted_dsn: str,
        sslmode: str = "prefer",
    ) -> Connection:
        conn = Connection(
            org_id=org_id,
            db_type=db_type,
            host=host,
            port=port,
            database_name=database_name,
            username=username,
            encrypted_dsn=encrypted_dsn,
            sslmode=sslmode,
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
        await self.db.flush()
        return conn

    async def delete(self, connection_id: UUID) -> bool:
        conn = await self.get_by_id(connection_id)
        if conn is None:
            return False
        await self.db.delete(conn)
        await self.db.flush()
        return True
