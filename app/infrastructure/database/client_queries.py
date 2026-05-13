"""Live queries against the client's external database.

Security guarantees:
- All client connections are READ-ONLY at the session level (SET TRANSACTION READ ONLY)
- Statement timeout of 30s prevents runaway queries
- Connections are ephemeral (created per-request, disposed immediately after)
- SQL identifiers are regex-validated and quoted
- Client data is never persisted to Pulse's database

SchemaMapping is queried separately from Pulse DB — it never touches the client engine.
"""

import logging
import re
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Any, AsyncIterator

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine

logger = logging.getLogger(__name__)

# Statement timeout for client DB queries (seconds)
_QUERY_TIMEOUT_S = 30

from app.infrastructure.crypto import decrypt_dsn
from app.infrastructure.database.models.connection import Connection
from app.infrastructure.database.models.schema_mapping import SchemaMapping


def _to_async_url(dsn: str) -> str:
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    if dsn.startswith("postgres://"):
        return dsn.replace("postgres://", "postgresql+asyncpg://", 1)
    if dsn.startswith("mysql://"):
        return dsn.replace("mysql://", "mysql+aiomysql://", 1)
    return dsn


def _connect_args(url: str) -> dict:
    if url.startswith("mysql+aiomysql://"):
        return {"connect_timeout": 10}
    return {"timeout": 10}


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(value: str | None, label: str) -> str:
    if not value or not _IDENTIFIER.fullmatch(value):
        raise ClientDBError(f"Invalid {label}: {value!r}")
    return value


def _quote_identifier(value: str, db_type: str | None) -> str:
    value = _validate_identifier(value, "SQL identifier")
    if db_type == "mysql":
        return f"`{value}`"
    return f'"{value}"'


def _schema_columns_sql(db_type: str | None) -> str:
    if db_type == "mysql":
        return (
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :tname "
            "ORDER BY ordinal_position"
        )
    return (
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = :tname "
        "ORDER BY ordinal_position"
    )


class ClientDBError(Exception):
    """Raised when the client DB is not available or misconfigured."""


async def get_schema_mapping(db: AsyncSession, org_id) -> SchemaMapping:
    """Return the org's schema mapping from Pulse DB, or raise ClientDBError."""
    result = await db.execute(
        select(SchemaMapping).where(SchemaMapping.org_id == org_id).limit(1)
    )
    mapping = result.scalar_one_or_none()
    if not mapping:
        raise ClientDBError("No schema mapping configured for this organization")
    return mapping


async def _get_client_engine(db: AsyncSession, org_id) -> tuple[AsyncEngine, Connection]:
    """Create a temporary async engine pointed at the org's client DB."""
    result = await db.execute(
        select(Connection).where(Connection.org_id == org_id).limit(1)
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise ClientDBError("No connection configured for this organization")
    dsn = decrypt_dsn(conn.encrypted_dsn)
    url = _to_async_url(dsn)
    engine = create_async_engine(url, connect_args=_connect_args(url))
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
            db_type = getattr(conn, "db_type", None)
            if db_type == "mysql":
                await client_conn.execute(text("SET SESSION TRANSACTION READ ONLY"))
                await client_conn.execute(
                    text(f"SET max_execution_time = {_QUERY_TIMEOUT_S * 1000}")
                )
            else:
                # PostgreSQL (default)
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
            logger.warning(
                "Failed to set read-only/timeout on client connection (proceeding): %s", e
            )
        yield client_conn


async def fetch_entities(
    db: AsyncSession, org_id, mapping: SchemaMapping
) -> list[dict]:
    """Fetch all entity rows from the client DB using the orgʼs schema mapping."""
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
            sql = (
                f"SELECT {', '.join(quoted_cols)} "
                f"FROM {_quote_identifier(table_name, _conn.db_type)}"
            )
            result = await client_conn.execute(text(sql))
            rows = result.all()
            return [dict(zip(cols, row)) for row in rows]
    finally:
        await engine.dispose()


async def fetch_entity_by_id(
    db: AsyncSession, org_id, entity_id: str, mapping: SchemaMapping
) -> dict | None:
    """Fetch a single entity from the client DB by ID."""
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

            quoted_cols = [_quote_identifier(c, _conn.db_type) for c in all_cols]
            sql = (
                f"SELECT {', '.join(quoted_cols)} FROM "
                f"{_quote_identifier(mapping.entity_table, _conn.db_type)} "
                f"WHERE {_quote_identifier(mapping.entity_id_col, _conn.db_type)} = :eid"
            )
            result = await client_conn.execute(text(sql), {"eid": entity_id})
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

            quoted_cols = [_quote_identifier(c, _conn.db_type) for c in cols]
            sql = (
                f"SELECT {', '.join(quoted_cols)} "
                f"FROM {_quote_identifier(table_name, _conn.db_type)} "
                f"WHERE {_quote_identifier(id_col, _conn.db_type)} = :eid "
                f"ORDER BY {_quote_identifier(ts_col, _conn.db_type)} DESC "
                "LIMIT :limit"
            )
            result = await client_conn.execute(text(sql), {"eid": entity_id, "limit": limit})
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

    signal_keys = list(signal_columns.keys())
    col_to_signal = {v: k for k, v in signal_columns.items()}

    signal_values: dict[str, list[float]] = {k: [] for k in signal_keys}
    for entity in entities:
        for col, signal_key in col_to_signal.items():
            val = entity.get(col)
            signal_values[signal_key].append(float(val) if val is not None else 0.0)

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
        for col, signal_key in col_to_signal.items():
            raw = float(entity.get(col, 0) or 0)
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
