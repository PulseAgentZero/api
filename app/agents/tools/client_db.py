"""Public wrappers around client-DB primitives used by agent tools.

`client_queries.py` is the battle-tested core of Pulse's live-data engine
and its low-level helpers are kept underscore-prefixed to discourage
ad-hoc reuse. This module exposes a small, public surface that agent tools
can depend on without reaching into private names.
"""

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.infrastructure.database.client_queries import (
    _get_client_engine as _impl_get_client_engine,
    _quote_identifier as _impl_quote_identifier,
    _safe_client_connection as _impl_safe_client_connection,
    _schema_columns_sql as _impl_schema_columns_sql,
    _validate_identifier as _impl_validate_identifier,
)
from app.infrastructure.database.models.connection import Connection


async def open_client_engine(
    db: AsyncSession, org_id: UUID
) -> tuple[AsyncEngine, Connection]:
    """Open an async engine pointed at the org's client database.

    Caller owns disposal (`await engine.dispose()`).
    """
    return await _impl_get_client_engine(db, org_id)


def safe_client_connection(
    engine: AsyncEngine, conn: Connection,
) -> asynccontextmanager:
    """Open a read-only, time-bounded connection to the client DB.

    Enforces READ ONLY session mode and statement timeout.
    Use this instead of raw `engine.connect()` for all client queries.
    """
    return _impl_safe_client_connection(engine, conn)


def quote_identifier(value: str, db_type: str | None) -> str:
    """Quote a SQL identifier safely for the given client DB dialect."""
    return _impl_quote_identifier(value, db_type)


def validate_identifier(value: str | None, label: str) -> str:
    """Validate an unquoted SQL identifier; raise ClientDBError on bad input."""
    return _impl_validate_identifier(value, label)


def schema_columns_sql(db_type: str | None) -> str:
    """SQL that returns ordered column names for a table in the client DB."""
    return _impl_schema_columns_sql(db_type)


__all__ = [
    "open_client_engine",
    "safe_client_connection",
    "quote_identifier",
    "validate_identifier",
    "schema_columns_sql",
]

