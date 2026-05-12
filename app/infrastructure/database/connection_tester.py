from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.api.schemas.connection import ColumnInfo, TableInfo


def _to_async_url(dsn: str) -> str:
    """Convert a sync DSN (postgresql://) to async (postgresql+asyncpg://)."""
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    if dsn.startswith("postgres://"):
        return dsn.replace("postgres://", "postgresql+asyncpg://", 1)
    return dsn


async def test_connection(dsn: str) -> tuple[bool, str, str | None]:
    """Try connecting and running SELECT version(). Returns (success, message, db_version)."""
    url = _to_async_url(dsn)
    engine: AsyncEngine | None = None
    try:
        engine = create_async_engine(url, connect_args={"timeout": 10})
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version()"))
            db_version = result.scalar_one()
        return True, "Connection successful", db_version
    except Exception as exc:
        return False, str(exc), None
    finally:
        if engine:
            await engine.dispose()


async def introspect_schema(dsn: str) -> list[TableInfo]:
    """Introspect the remote database and return all tables with their columns."""
    url = _to_async_url(dsn)
    engine: AsyncEngine | None = None
    try:
        engine = create_async_engine(url, connect_args={"timeout": 10})
        async with engine.connect() as conn:
            tables_result = await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' ORDER BY table_name"
                )
            )
            table_names = [row[0] for row in tables_result.all()]

            tables: list[TableInfo] = []
            for table_name in table_names:
                cols_result = await conn.execute(
                    text(
                        "SELECT column_name, data_type, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = :tname "
                        "ORDER BY ordinal_position"
                    ),
                    {"tname": table_name},
                )
                columns = [
                    ColumnInfo(name=row[0], data_type=row[1], nullable=row[2] == "YES")
                    for row in cols_result.all()
                ]
                tables.append(TableInfo(name=table_name, columns=columns))
            return tables
    finally:
        if engine:
            await engine.dispose()
