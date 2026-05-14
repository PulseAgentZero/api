import asyncio
import logging
import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.api.schemas.connection import ColumnInfo, TableInfo
from app.infrastructure.connectors.connector_health import test_pulse_api_payload
from app.infrastructure.connectors.payload import parse_pulse_api_payload
from app.infrastructure.database.sql_connect import (
    connect_args_for_async_url,
    is_likely_async_sqlalchemy_url,
    sync_dsn_to_async_sqlalchemy_url,
    uses_sync_sqlalchemy_engine,
)

logger = logging.getLogger(__name__)

_QUERY_TIMEOUT_S = 30


async def _set_session_guards(conn, url: str) -> None:
    """Set read-only + statement timeout on a client DB connection (best-effort)."""
    try:
        if url.startswith("mysql+aiomysql://"):
            await conn.execute(text("SET SESSION TRANSACTION READ ONLY"))
            await conn.execute(
                text(f"SET max_execution_time = {_QUERY_TIMEOUT_S * 1000}")
            )
        elif url.startswith("sqlite+aiosqlite"):
            await conn.execute(text("PRAGMA query_only=ON"))
        elif url.startswith("mssql+aioodbc"):
            await conn.execute(text("SET LOCK_TIMEOUT 30000"))
        else:
            await conn.execute(
                text("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            )
            await conn.execute(
                text(f"SET statement_timeout = '{_QUERY_TIMEOUT_S}s'")
            )
    except Exception as e:
        logger.warning("Failed to set session guards: %s", e)


def _version_sql(url: str) -> str:
    low = url.lower()
    if "mysql" in low:
        return "SELECT VERSION() AS v"
    if "sqlite" in low:
        return "SELECT sqlite_version() AS v"
    if "mssql" in low:
        return "SELECT @@VERSION AS v"
    return "SELECT version() AS v"


def _sync_sqlalchemy_smoke(dsn: str) -> tuple[bool, str, str | None]:
    from sqlalchemy import create_engine, text as t

    eng = None
    try:
        eng = create_engine(dsn, pool_pre_ping=True, future=True)
        with eng.connect() as c:
            low = dsn.lower()
            if "snowflake" in low:
                row = c.execute(t("SELECT CURRENT_VERSION()")).scalar_one()
            elif "bigquery" in low or "databricks" in low:
                row = c.execute(t("SELECT 1")).scalar_one()
            else:
                row = c.execute(t("SELECT 1")).scalar_one()
        return True, "Connection successful", str(row)[:500]
    except Exception as exc:
        return False, str(exc), None
    finally:
        if eng is not None:
            eng.dispose()


async def test_connection(
    dsn_or_blob: str,
    sslmode: str | None = None,
) -> tuple[bool, str, str | None]:
    """Ping remote data store (SQL URL, warehouse URL, or encrypted API JSON blob)."""
    raw = dsn_or_blob.strip()
    api = parse_pulse_api_payload(raw)
    if api is not None:
        return await test_pulse_api_payload(api)

    if uses_sync_sqlalchemy_engine(raw):
        return await asyncio.to_thread(_sync_sqlalchemy_smoke, raw)
    url = sync_dsn_to_async_sqlalchemy_url(raw)
    if not is_likely_async_sqlalchemy_url(url):
        return await asyncio.to_thread(_sync_sqlalchemy_smoke, raw)
    engine: AsyncEngine | None = None
    try:
        engine = create_async_engine(url, connect_args=connect_args_for_async_url(url, sslmode))
        async with engine.connect() as conn:
            await _set_session_guards(conn, url)
            result = await conn.execute(text(_version_sql(url)))
            db_version = result.scalar_one()
        return True, "Connection successful", str(db_version)
    except Exception as exc:
        return False, str(exc), None
    finally:
        if engine:
            await engine.dispose()


async def introspect_schema(dsn: str, sslmode: str | None = None) -> list[TableInfo]:
    """Introspect tables + columns."""
    raw = dsn.strip()
    if parse_pulse_api_payload(raw) is not None:
        raise ValueError("Schema introspection is not available for API or object-storage connectors")
    if uses_sync_sqlalchemy_engine(raw):
        return await asyncio.to_thread(_introspect_schema_sync, raw)
    url = sync_dsn_to_async_sqlalchemy_url(raw)
    if not is_likely_async_sqlalchemy_url(url):
        raise ValueError(
            "Schema introspection requires async SQLAlchemy support for this URL; "
            "try a postgres/mysql/sqlite/mssql URL or use warehouse sync introspection."
        )
    engine: AsyncEngine | None = None
    try:
        engine = create_async_engine(url, connect_args=connect_args_for_async_url(url, sslmode))
        async with engine.connect() as conn:
            await _set_session_guards(conn, url)
            low = url.lower()
            if low.startswith("mysql+aiomysql://"):
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
            elif "sqlite" in low:
                tables_sql = (
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
                columns_sql = ""
            elif "mssql" in low:
                tables_sql = (
                    "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_SCHEMA NOT IN ('INFORMATION_SCHEMA','sys') "
                    "AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME"
                )
                columns_sql = (
                    "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE "
                    "FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = SCHEMA_NAME() AND TABLE_NAME = :tname "
                    "ORDER BY ORDINAL_POSITION"
                )
            else:
                tables_sql = (
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = current_schema() AND table_type='BASE TABLE' "
                    "ORDER BY table_name"
                )
                columns_sql = (
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = :tname "
                    "ORDER BY ordinal_position"
                )
            tables_result = await conn.execute(text(tables_sql))
            table_names = [row[0] for row in tables_result.all()]

            tables: list[TableInfo] = []
            for table_name in table_names:
                if "sqlite" in low:
                    safe = str(table_name).replace('"', '""')
                    cols_result = await conn.execute(text(f'PRAGMA table_info("{safe}")'))
                    columns = [
                        ColumnInfo(name=row[1], data_type=str(row[2]), nullable=not bool(row[3]))
                        for row in cols_result.all()
                    ]
                else:
                    cols_result = await conn.execute(text(columns_sql), {"tname": table_name})
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


def _introspect_schema_sync(dsn: str) -> list[TableInfo]:
    from sqlalchemy import create_engine, text as t

    eng = None
    try:
        eng = create_engine(dsn, pool_pre_ping=True, future=True)
        with eng.connect() as conn:
            low = dsn.lower()
            if "snowflake" in low:
                rows = conn.execute(
                    t(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = current_schema() ORDER BY table_name"
                    )
                ).all()
            elif "bigquery" in low:
                rows = conn.execute(
                    t(
                        "SELECT table_name FROM INFORMATION_SCHEMA.TABLES "
                        "WHERE table_type = 'BASE TABLE' ORDER BY table_name"
                    )
                ).all()
            else:
                rows = []
            tables: list[TableInfo] = []
            for (table_name,) in rows:
                cols = conn.execute(
                    t(
                        "SELECT column_name, data_type, is_nullable "
                        "FROM information_schema.columns WHERE table_name = :t "
                    ),
                    {"t": table_name},
                ).all()
                columns = [
                    ColumnInfo(name=r[0], data_type=r[1], nullable=r[2] == "YES") for r in cols
                ]
                tables.append(TableInfo(name=table_name, columns=columns))
            return tables
    finally:
        if eng is not None:
            eng.dispose()


_SAFE_SQL_IDENT = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def validate_sql_identifier(name: str) -> str:
    if not _SAFE_SQL_IDENT.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


async def preview_table_rows(
    dsn: str, table_name: str, limit: int = 50, *, sslmode: str | None = None
) -> list[dict[str, object]]:
    """Return up to `limit` rows as plain dicts."""
    raw = dsn.strip()
    if parse_pulse_api_payload(raw) is not None:
        raise ValueError("Preview is not available for API or object-storage connectors")
    t = validate_sql_identifier(table_name)
    lim = max(1, min(int(limit), 500))
    if uses_sync_sqlalchemy_engine(raw):
        return await asyncio.to_thread(_preview_table_rows_sync, raw, t, lim)
    url = sync_dsn_to_async_sqlalchemy_url(raw)
    engine: AsyncEngine | None = None
    try:
        engine = create_async_engine(url, connect_args=connect_args_for_async_url(url, sslmode))
        async with engine.connect() as conn:
            if url.startswith("mysql+aiomysql://"):
                q = text(f"SELECT * FROM `{t}` LIMIT :lim").bindparams(lim=lim)
            elif "sqlite" in url.lower():
                q = text(f'SELECT * FROM "{t}" LIMIT :lim').bindparams(lim=lim)
            elif "mssql" in url.lower():
                q = text(f"SELECT TOP (:lim) * FROM [{t}]").bindparams(lim=lim)
            else:
                q = text(f'SELECT * FROM "{t}" LIMIT :lim').bindparams(lim=lim)
            result = await conn.execute(q)
            cols = list(result.keys())
            rows = []
            for row in result.fetchall():
                rows.append({cols[i]: row[i] for i in range(len(cols))})
            return rows
    finally:
        if engine:
            await engine.dispose()


def _preview_table_rows_sync(dsn: str, table: str, lim: int) -> list[dict[str, object]]:
    from sqlalchemy import create_engine, text as tx

    eng = None
    try:
        eng = create_engine(dsn, pool_pre_ping=True, future=True)
        with eng.connect() as c:
            q = tx(f'SELECT * FROM "{table}" LIMIT {int(lim)}')
            result = c.execute(q)
            cols = list(result.keys())
            return [{cols[i]: row[i] for i in range(len(cols))} for row in result.fetchall()]
    finally:
        if eng is not None:
            eng.dispose()
