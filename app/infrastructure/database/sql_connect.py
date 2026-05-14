"""Normalize client DSNs to SQLAlchemy async URLs and driver connect args.

Covers PostgreSQL, MySQL, Microsoft SQL Server (ODBC), SQLite, and passes
through Snowflake / BigQuery / Databricks URLs for sync test paths.
"""

from __future__ import annotations

import os

_DEFAULT_MSSQL_ODBC_QUERY = (
    "driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes"
)


def mssql_odbc_query() -> str:
    d = os.getenv("MSSQL_ODBC_DRIVER", "").strip()
    if d:
        return f"driver={d.replace(' ', '+')}&TrustServerCertificate=yes"
    return _DEFAULT_MSSQL_ODBC_QUERY


def sync_dsn_to_async_sqlalchemy_url(dsn: str) -> str:
    """Map a stored (sync-style) DSN to an async SQLAlchemy URL where supported."""
    s = dsn.strip()
    if not s:
        return s
    lower = s.lower()
    if lower.startswith("postgresql://"):
        return s.replace("postgresql://", "postgresql+asyncpg://", 1)
    if lower.startswith("postgres://"):
        return s.replace("postgres://", "postgresql+asyncpg://", 1)
    if lower.startswith("mysql://"):
        return s.replace("mysql://", "mysql+aiomysql://", 1)
    if lower.startswith("mssql://") or lower.startswith("mssql+"):
        if "+aioodbc" in lower:
            return s
        if "://" in s:
            _scheme, rest = s.split("://", 1)
            return f"mssql+aioodbc://{rest}"
        return s
    if lower.startswith("sqlite://"):
        if "+aiosqlite" in lower:
            return s
        return s.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return s


def connect_args_for_async_url(url: str, sslmode: str | None = None) -> dict:
    args: dict = {}
    lower = url.lower()
    if lower.startswith("mysql+aiomysql://"):
        args["connect_timeout"] = 10
        if sslmode and sslmode != "prefer":
            args["ssl"] = {"ssl": True}
    elif lower.startswith("sqlite+aiosqlite"):
        args["timeout"] = float(10)
    elif lower.startswith("mssql+aioodbc"):
        args["timeout"] = 10
    else:
        args["timeout"] = 10
        if sslmode and sslmode != "prefer":
            args["ssl"] = sslmode
    return args


def is_likely_async_sqlalchemy_url(url: str) -> bool:
    lower = url.lower()
    return any(
        lower.startswith(p)
        for p in (
            "postgresql+asyncpg://",
            "mysql+aiomysql://",
            "sqlite+aiosqlite://",
            "mssql+aioodbc://",
        )
    )


def warehouse_requires_sync_engine(url: str) -> bool:
    lower = url.lower()
    return any(
        x in lower
        for x in (
            "snowflake://",
            "bigquery://",
            "databricks://",
            "redshift+",
        )
    )


def uses_sync_sqlalchemy_engine(url: str) -> bool:
    """Whether SQLAlchemy should use a synchronous engine for connectivity tests."""
    return warehouse_requires_sync_engine(url)
