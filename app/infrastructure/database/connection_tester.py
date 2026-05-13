import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.api.schemas.connection import ColumnInfo, TableInfo

logger = logging.getLogger(__name__)

_QUERY_TIMEOUT_S = 30


async def _set_session_guards(conn, url: str) -> None:
    """Set read-only + statement timeout on a client DB connection."""
    try:
        if url.startswith("mysql+aiomysql://"):
            await conn.execute(text("SET SESSION TRANSACTION READ ONLY"))
            await conn.execute(
                text(f"SET max_execution_time = {_QUERY_TIMEOUT_S * 1000}")
            )
        else:
            await conn.execute(
                text("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            )
            await conn.execute(
                text(f"SET statement_timeout = '{_QUERY_TIMEOUT_S}s'")
            )
    except Exception as e:
        logger.warning("Failed to set session guards: %s", e)


def _to_async_url(dsn: str) -> str:
    """Convert a sync DSN to its async SQLAlchemy driver URL."""
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    if dsn.startswith("postgres://"):
        return dsn.replace("postgres://", "postgresql+asyncpg://", 1)
    if dsn.startswith("mysql://"):
        return dsn.replace("mysql://", "mysql+aiomysql://", 1)
    return dsn


def _connect_args(url: str, sslmode: str | None = None) -> dict:
    args: dict = {}
    if url.startswith("mysql+aiomysql://"):
        args["connect_timeout"] = 10
        if sslmode and sslmode != "prefer":
            args["ssl"] = {"ssl": True}
    else:
        args["timeout"] = 10
        if sslmode and sslmode != "prefer":
            args["ssl"] = sslmode
    return args


async def test_connection(dsn: str, sslmode: str | None = None) -> tuple[bool, str, str | None]:
    """Try connecting and running SELECT version(). Returns (success, message, db_version)."""
    url = _to_async_url(dsn)
    engine: AsyncEngine | None = None
    try:
        engine = create_async_engine(url, connect_args=_connect_args(url, sslmode))
        async with engine.connect() as conn:
            await _set_session_guards(conn, url)
            result = await conn.execute(text("SELECT version()"))
            db_version = result.scalar_one()
        return True, "Connection successful", db_version
    except Exception as exc:
        return False, str(exc), None
    finally:
        if engine:
            await engine.dispose()


async def introspect_schema(dsn: str, sslmode: str | None = None) -> list[TableInfo]:
    """Introspect the remote database and return all tables with their columns."""
    url = _to_async_url(dsn)
    engine: AsyncEngine | None = None
    try:
        engine = create_async_engine(url, connect_args=_connect_args(url, sslmode))
        async with engine.connect() as conn:
            await _set_session_guards(conn, url)
            if url.startswith("mysql+aiomysql://"):
                tables_sql = (
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() ORDER BY table_name"
                )
                columns_sql = (
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() AND table_name = :tname "
                    "ORDER BY ordinal_position"
                )
            else:
                tables_sql = (
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' ORDER BY table_name"
                )
                columns_sql = (
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :tname "
                    "ORDER BY ordinal_position"
                )
            tables_result = await conn.execute(
                text(tables_sql)
            )
            table_names = [row[0] for row in tables_result.all()]

            tables: list[TableInfo] = []
            for table_name in table_names:
                cols_result = await conn.execute(
                    text(columns_sql),
                    {"tname": table_name},
                )
                columns = [
                    ColumnInfo(name=row[0], data_type=row[1], nullable=row[2] == "YES")
                    for row in cols_result.all()
                ]
                tables.append(TableInfo(name=table_name, columns=columns))
            return tables
    except Exception as exc:
        logger.error("Schema introspection failed: %s", exc)
        raise
    finally:
        if engine:
            await engine.dispose()
