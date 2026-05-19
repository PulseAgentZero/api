"""Live queries against the client's external database.

Security guarantees (when the client database cooperates):
- Session is READ-ONLY where supported (SET TRANSACTION READ ONLY / MySQL equivalent)
- Statement timeout of 30s prevents runaway queries
- Connections are ephemeral (created per-request, disposed immediately after)
- SQL identifiers are regex-validated and quoted
- Client data is never persisted to Pulse's database

If read-only or timeout session variables cannot be applied, the connection is
refused with :class:`ClientDBError` rather than proceeding without those guards.

SchemaMapping is queried separately from Pulse DB — it never touches the client engine.
"""

import logging
import re
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Any, AsyncIterator
from uuid import UUID

from sqlalchemy import case, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine

logger = logging.getLogger(__name__)

# Statement timeout for client DB queries (seconds)
_QUERY_TIMEOUT_S = 30

# Hard cap on rows returned from a single entity-table scan (pipeline / services).
_MAX_ENTITY_FETCH_ROWS = 50_000

from app.infrastructure.crypto import decrypt_dsn
from app.infrastructure.connectors.payload import parse_pulse_api_payload
from app.infrastructure.database.models.connection import Connection
from app.infrastructure.database.models.schema_mapping import SchemaMapping
from app.infrastructure.database.sql_connect import (
    connect_args_for_async_url,
    sync_dsn_to_async_sqlalchemy_url,
)


def _to_async_url(dsn: str) -> str:
    return sync_dsn_to_async_sqlalchemy_url(dsn)


def _connect_args(url: str, sslmode: str | None = None) -> dict:
    return connect_args_for_async_url(url, sslmode)


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(value: str | None, label: str) -> str:
    if not value or not _IDENTIFIER.fullmatch(value):
        raise ClientDBError(f"Invalid {label}: {value!r}")
    return value


def _norm_client_db_type(db_type: str | None) -> str | None:
    if db_type == "postgres":
        return "postgresql"
    return db_type


def _quote_identifier(value: str, db_type: str | None) -> str:
    value = _validate_identifier(value, "SQL identifier")
    dt = _norm_client_db_type(db_type)
    if dt == "mysql":
        return f"`{value}`"
    if dt == "mssql":
        return f"[{value}]"
    return f'"{value}"'


def _schema_columns_sql(db_type: str | None) -> str:
    dt = _norm_client_db_type(db_type)
    if dt == "mysql":
        return (
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :tname "
            "ORDER BY ordinal_position"
        )
    if dt == "sqlite":
        return (
            "SELECT name AS column_name FROM pragma_table_info(:tname) ORDER BY cid"
        )
    if dt == "mssql":
        return (
            "SELECT COLUMN_NAME AS column_name FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = SCHEMA_NAME() AND TABLE_NAME = :tname "
            "ORDER BY ORDINAL_POSITION"
        )
    return (
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = :tname "
        "ORDER BY ordinal_position"
    )


def _column_data_type_sql(db_type: str | None) -> str:
    """Return SQL that fetches the data_type for a specific column in a table."""
    dt = _norm_client_db_type(db_type)
    if dt == "mysql":
        return (
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :tname "
            "AND column_name = :cname LIMIT 1"
        )
    if dt == "sqlite":
        return (
            "SELECT type AS data_type FROM pragma_table_info(:tname) "
            "WHERE name = :cname LIMIT 1"
        )
    if dt == "mssql":
        return (
            "SELECT DATA_TYPE AS data_type FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = SCHEMA_NAME() AND TABLE_NAME = :tname "
            "AND COLUMN_NAME = :cname"
        )
    return (
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = :tname "
        "AND column_name = :cname LIMIT 1"
    )


_INTEGER_TYPE_NAMES = frozenset({
    "integer", "int", "int4", "int8", "int2",
    "bigint", "smallint", "tinyint", "serial", "bigserial",
    "mediumint", "number",
})

_UUID_TYPE_NAMES = frozenset({"uuid"})


def _coerce_entity_id(entity_id: str, data_type: str | None) -> object:
    """Cast the entity_id string to the correct Python type for SQL binding.

    This prevents asyncpg DataError when the client DB column is an integer
    but the chat agent passes a string like '628'.
    """
    if not data_type:
        return entity_id
    dt_lower = data_type.lower().strip()
    if dt_lower in _INTEGER_TYPE_NAMES:
        try:
            return int(entity_id)
        except (ValueError, TypeError):
            return entity_id
    if dt_lower in _UUID_TYPE_NAMES:
        import uuid as _uuid
        try:
            return _uuid.UUID(entity_id)
        except (ValueError, TypeError):
            return entity_id
    return entity_id


async def _detect_id_column_type(
    client_conn: AsyncConnection, db_type: str | None,
    table_name: str, column_name: str,
) -> str | None:
    """Best-effort detection of the entity ID column's data type."""
    try:
        sql = _column_data_type_sql(db_type)
        result = await client_conn.execute(
            text(sql), {"tname": table_name, "cname": column_name},
        )
        row = result.one_or_none()
        return str(row[0]).lower().strip() if row else None
    except Exception as exc:
        logger.debug("[client_queries] column type detection failed (non-fatal): %s", exc)
        return None


class ClientDBError(Exception):
    """Raised when the client DB is not available or misconfigured."""


async def get_schema_mapping(
    db: AsyncSession,
    org_id,
    mapping_id: UUID | None = None,
) -> SchemaMapping:
    """Return the org's schema mapping from Pulse DB, or raise ClientDBError."""
    stmt = select(SchemaMapping).where(SchemaMapping.org_id == org_id)
    if mapping_id is not None:
        stmt = stmt.where(SchemaMapping.id == mapping_id)
    else:
        stmt = stmt.order_by(
            SchemaMapping.is_active.desc(),
            SchemaMapping.updated_at.desc(),
            SchemaMapping.created_at.desc(),
        )
    result = await db.execute(
        stmt.limit(1)
    )
    mapping = result.scalar_one_or_none()
    if not mapping:
        if mapping_id is not None:
            raise ClientDBError("Requested schema mapping is not configured for this organization")
        raise ClientDBError("No schema mapping configured for this organization")
    return mapping


async def _get_client_engine(db: AsyncSession, org_id) -> tuple[AsyncEngine, Connection]:
    """Create a temporary async engine pointed at the org's client DB.

    When multiple connections exist, prefer a non-deleted row with ``status ==
    "active"``, then the most recently updated.
    """
    result = await db.execute(
        select(Connection)
        .where(Connection.org_id == org_id, Connection.deleted_at.is_(None))
        .order_by(
            case((Connection.status == "active", 0), else_=1),
            Connection.updated_at.desc(),
            Connection.created_at.desc(),
        )
        .limit(1)
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise ClientDBError("No connection configured for this organization")
    dsn = decrypt_dsn(conn.encrypted_dsn)
    if parse_pulse_api_payload(dsn) is not None:
        raise ClientDBError(
            "This org connection is an API or object-store connector. "
            "Use a SQL database (Postgres, MySQL, SQLite, SQL Server, Redshift) for live SQL entity mapping, "
            "or upload CSV for file-based workflows."
        )
    url = _to_async_url(dsn)
    engine = create_async_engine(url, connect_args=_connect_args(url, conn.sslmode))
    return engine, conn


@asynccontextmanager
async def _safe_client_connection(
    engine: AsyncEngine, conn: Connection,
) -> AsyncIterator[AsyncConnection]:
    """Open a read-only, time-bounded connection to the client DB.

    Security enforced at the database session level:
    - READ ONLY: prevents INSERT, UPDATE, DELETE, DROP, etc.
    - Statement timeout: kills queries exceeding the time limit

    Even if the connected DB user has full write permissions, the
    READ ONLY session mode physically prevents data mutation.
    """
    async with engine.connect() as client_conn:
        try:
            db_type = _norm_client_db_type(getattr(conn, "db_type", None)) or "postgresql"
            if db_type == "mysql":
                await client_conn.execute(text("SET SESSION TRANSACTION READ ONLY"))
                await client_conn.execute(
                    text(f"SET max_execution_time = {_QUERY_TIMEOUT_S * 1000}")
                )
            elif db_type == "mssql":
                await client_conn.execute(
                    text(f"SET LOCK_TIMEOUT {int(_QUERY_TIMEOUT_S * 1000)}")
                )
            elif db_type == "sqlite":
                await client_conn.execute(text("PRAGMA query_only = ON"))
                await client_conn.execute(
                    text(f"PRAGMA busy_timeout = {int(_QUERY_TIMEOUT_S * 1000)}")
                )
            else:
                # PostgreSQL, Redshift, and other Postgres-compatible engines
                await client_conn.execute(
                    text("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
                )
                await client_conn.execute(
                    text(f"SET statement_timeout = '{_QUERY_TIMEOUT_S}s'")
                )
            logger.debug(
                "Client DB connection opened: READ ONLY, timeout=%ds, db_type=%s",
                _QUERY_TIMEOUT_S, db_type,
            )
        except Exception as e:
            logger.error(
                "Refusing client DB session: could not enforce read-only or statement timeout: %s",
                e,
            )
            raise ClientDBError(
                "Could not enforce read-only session or statement timeout on the client database"
            ) from e
        yield client_conn


async def fetch_entities(
    db: AsyncSession, org_id, mapping: SchemaMapping
) -> list[dict]:
    """Fetch entity rows from the client DB using the org's schema mapping.

    At most :data:`_MAX_ENTITY_FETCH_ROWS` rows are returned per call.
    """
    engine, _conn = await _get_client_engine(db, org_id)
    try:
        async with _safe_client_connection(engine, _conn) as client_conn:
            table_name = _validate_identifier(mapping.entity_table, "entity table")
            cols = [_validate_identifier(mapping.entity_id_col, "entity ID column")]
            if mapping.entity_name_col:
                cols.append(_validate_identifier(mapping.entity_name_col, "entity name column"))
            for col_name in (mapping.signal_columns or {}).values():
                col_name = _validate_identifier(col_name, "signal column")
                if col_name not in cols:
                    cols.append(col_name)

            quoted_cols = [_quote_identifier(c, _conn.db_type) for c in cols]
            q_table = _quote_identifier(table_name, _conn.db_type)
            col_list = ", ".join(quoted_cols)
            if _norm_client_db_type(_conn.db_type) == "mssql":
                sql = f"SELECT TOP (:lim) {col_list} FROM {q_table}"
            else:
                sql = f"SELECT {col_list} FROM {q_table} LIMIT :lim"
            result = await client_conn.execute(text(sql), {"lim": _MAX_ENTITY_FETCH_ROWS})
            rows = result.all()
            return [dict(zip(cols, row)) for row in rows]
    finally:
        await engine.dispose()


async def fetch_entity_by_id(
    db: AsyncSession, org_id, entity_id: str, mapping: SchemaMapping
) -> dict | None:
    """Fetch a single entity from the client DB by ID.

    Auto-detects the entity ID column's data type and casts the bind
    parameter accordingly, preventing asyncpg DataError when the column
    is an integer but the caller passes a string.
    """
    engine, _conn = await _get_client_engine(db, org_id)
    try:
        async with _safe_client_connection(engine, _conn) as client_conn:
            cols_result = await client_conn.execute(
                text(_schema_columns_sql(_conn.db_type)),
                {"tname": mapping.entity_table},
            )
            all_cols = [row[0] for row in cols_result.all()]
            if not all_cols:
                raise ClientDBError("Mapped entity table was not found in the client database")

            # Detect entity ID column type so we can cast the bind param.
            id_col_type = await _detect_id_column_type(
                client_conn, _conn.db_type,
                mapping.entity_table, mapping.entity_id_col,
            )
            coerced_eid = _coerce_entity_id(entity_id, id_col_type)

            quoted_cols = [_quote_identifier(c, _conn.db_type) for c in all_cols]
            sql = (
                f"SELECT {', '.join(quoted_cols)} FROM "
                f"{_quote_identifier(mapping.entity_table, _conn.db_type)} "
                f"WHERE {_quote_identifier(mapping.entity_id_col, _conn.db_type)} = :eid"
            )
            result = await client_conn.execute(text(sql), {"eid": coerced_eid})
            row = result.one_or_none()
            if row is None:
                return None
            return dict(zip(all_cols, row))
    finally:
        await engine.dispose()


async def fetch_entity_trend(
    db: AsyncSession,
    org_id,
    entity_id: str,
    mapping: SchemaMapping,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Fetch timestamped signal values for one entity when a timestamp column is mapped."""

    if not mapping.timestamp_col:
        raise ClientDBError("No timestamp column configured for this organization")

    engine, _conn = await _get_client_engine(db, org_id)
    try:
        async with _safe_client_connection(engine, _conn) as client_conn:
            table_name = _validate_identifier(mapping.entity_table, "entity table")
            id_col = _validate_identifier(mapping.entity_id_col, "entity ID column")
            ts_col = _validate_identifier(mapping.timestamp_col, "timestamp column")
            cols = [ts_col]
            for col_name in (mapping.signal_columns or {}).values():
                col_name = _validate_identifier(col_name, "signal column")
                if col_name not in cols:
                    cols.append(col_name)

            # Detect entity ID column type for correct bind param casting.
            id_col_type = await _detect_id_column_type(
                client_conn, _conn.db_type, table_name, id_col,
            )
            coerced_eid = _coerce_entity_id(entity_id, id_col_type)

            quoted_cols = [_quote_identifier(c, _conn.db_type) for c in cols]
            q_table = _quote_identifier(table_name, _conn.db_type)
            q_id = _quote_identifier(id_col, _conn.db_type)
            q_ts = _quote_identifier(ts_col, _conn.db_type)
            col_list = ", ".join(quoted_cols)
            if _norm_client_db_type(_conn.db_type) == "mssql":
                sql = (
                    f"SELECT {col_list} FROM {q_table} WHERE {q_id} = :eid "
                    f"ORDER BY {q_ts} DESC OFFSET 0 ROWS FETCH NEXT :limit ROWS ONLY"
                )
            else:
                sql = (
                    f"SELECT {col_list} FROM {q_table} WHERE {q_id} = :eid "
                    f"ORDER BY {q_ts} DESC LIMIT :limit"
                )
            result = await client_conn.execute(text(sql), {"eid": coerced_eid, "limit": limit})
            rows = result.all()
            points = []
            for row in reversed(rows):
                values = dict(zip(cols, row))
                ts_value = values.pop(ts_col)
                if isinstance(ts_value, datetime | date):
                    ts = ts_value.isoformat()
                else:
                    ts = str(ts_value)
                points.append({"timestamp": ts, "values": values})
            return points
    finally:
        await engine.dispose()


def _try_float(val: object) -> float | None:
    """Parse a DB cell as float; return None for categorical / non-numeric values."""
    if val is None:
        return None
    if isinstance(val, bool):
        return float(val)
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        stripped = val.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def compute_risk(
    entities: list[dict],
    signal_columns: dict | None,
    risk_config: dict | None,
) -> list[dict]:
    """Add risk_score and risk_tier to each entity dict in place. Returns the list."""

    if not entities or not signal_columns:
        for e in entities:
            e["risk_score"] = 0.0
            e["risk_tier"] = "low"
        return entities

    col_to_signal = {v: k for k, v in signal_columns.items()}
    # Only score columns that contain numeric values (skip region names, labels, etc.).
    numeric_col_to_signal: dict[str, str] = {}
    for col, signal_key in col_to_signal.items():
        if any(_try_float(entity.get(col)) is not None for entity in entities):
            numeric_col_to_signal[col] = signal_key

    if not numeric_col_to_signal:
        for e in entities:
            e["risk_score"] = 0.0
            e["risk_tier"] = "low"
            e["signals"] = {}
        return entities

    signal_keys = list({k for k in numeric_col_to_signal.values()})

    signal_values: dict[str, list[float]] = {k: [] for k in signal_keys}
    for entity in entities:
        for col, signal_key in numeric_col_to_signal.items():
            parsed = _try_float(entity.get(col))
            signal_values[signal_key].append(parsed if parsed is not None else 0.0)

    mins = {}
    maxs = {}
    for k in signal_keys:
        vals = signal_values[k]
        mins[k] = min(vals)
        maxs[k] = max(vals)

    config = risk_config or {}
    weights = config.get("weights", {})
    if not weights:
        weights = {k: 1.0 for k in signal_keys}
    total_weight = sum(weights.get(k, 0) for k in signal_keys) or 1.0

    for entity in entities:
        signals = {}
        score = 0.0
        for col, signal_key in numeric_col_to_signal.items():
            parsed = _try_float(entity.get(col))
            raw = parsed if parsed is not None else 0.0
            signals[signal_key] = raw
            rng = maxs[signal_key] - mins[signal_key]
            normalized = _signal_score(
                raw,
                mins[signal_key],
                maxs[signal_key],
                signal_key,
                config,
            )
            score += normalized * weights.get(signal_key, 1.0)

        score = score / total_weight
        entity["risk_score"] = round(score, 4)
        entity["risk_tier"] = _tier(score, config)
        entity["signals"] = signals

    return entities


def _signal_score(
    raw: float,
    min_value: float,
    max_value: float,
    signal_key: str,
    risk_config: dict,
) -> float:
    signal_config = (risk_config.get("signals") or {}).get(signal_key) or {}
    direction = signal_config.get("direction", risk_config.get("direction", "higher"))
    direction = str(direction).lower()

    critical = signal_config.get("critical")
    high = signal_config.get("high")
    medium = signal_config.get("medium")
    thresholds = [v for v in (medium, high, critical) if isinstance(v, int | float)]

    if thresholds:
        if direction in {"lower", "below", "decrease", "inverse"}:
            if critical is not None and raw <= float(critical):
                return 1.0
            if high is not None and raw <= float(high):
                return 0.75
            if medium is not None and raw <= float(medium):
                return 0.5
            return 0.0
        if critical is not None and raw >= float(critical):
            return 1.0
        if high is not None and raw >= float(high):
            return 0.75
        if medium is not None and raw >= float(medium):
            return 0.5
        return 0.0

    rng = max_value - min_value
    normalized = (raw - min_value) / rng if rng > 0 else 0.0
    if direction in {"lower", "below", "decrease", "inverse"}:
        normalized = 1.0 - normalized
    return max(0.0, min(1.0, normalized))


def _tier(score: float, risk_config: dict | None = None) -> str:
    thresholds = (risk_config or {}).get("tier_thresholds", {})
    critical = float(thresholds.get("critical", 0.8))
    high = float(thresholds.get("high", 0.6))
    medium = float(thresholds.get("medium", 0.4))
    if score >= critical:
        return "critical"
    if score >= high:
        return "high"
    if score >= medium:
        return "medium"
    return "low"
