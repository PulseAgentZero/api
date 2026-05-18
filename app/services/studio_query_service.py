"""Pulse Studio — SQL execution service.

Security:
- _is_select_only: keyword blocklist rejects non-SELECT statements fast
- execute_studio_query wraps every execution in safe_client_connection which
  sets READ ONLY + statement_timeout at the DB session level (hard wall)
- _inject_limit caps rows at _MAX_ROWS regardless of user-supplied LIMIT
- _get_specific_engine validates connection.org_id == caller's org_id
- apply_params uses SQLAlchemy bind params — never string interpolation
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.agents.tools.client_db import open_client_engine, safe_client_connection
from app.api.errors import bad_request, not_found
from app.infrastructure.connectors.payload import parse_pulse_api_payload
from app.infrastructure.crypto import decrypt_dsn
from app.infrastructure.database.client_queries import ClientDBError
from app.infrastructure.database.models.connection import Connection
from app.infrastructure.database.sql_connect import (
    connect_args_for_async_url,
    sync_dsn_to_async_sqlalchemy_url,
)

logger = logging.getLogger(__name__)

_MAX_ROWS = 5000
_CACHE_TTL = 300  # 5 minutes
_CACHE_PREFIX = "studio:qcache:"

_DANGEROUS_KW = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE"
    r"|EXEC|EXECUTE|COPY|LOAD|CALL|MERGE|REPLACE"
    r"|INTO\s+OUTFILE|INTO\s+DUMPFILE)\b",
    re.IGNORECASE,
)
_STRIP_LINE_COMMENTS = re.compile(r"--[^\n]*")
_STRIP_BLOCK_COMMENTS = re.compile(r"/\*.*?\*/", re.DOTALL)
_STRIP_QUOTED = re.compile(r"'[^']*'")

# Matches {{param_name}} placeholders in SQL
_PARAM_PATTERN = re.compile(r"\{\{(\w+)\}\}")


# ── SQL validation ────────────────────────────────────────────────────────────

def _is_select_only(sql: str) -> bool:
    """Return True only if sql is a safe SELECT/WITH statement."""
    cleaned = _STRIP_LINE_COMMENTS.sub(" ", sql)
    cleaned = _STRIP_BLOCK_COMMENTS.sub(" ", cleaned)
    no_strings = _STRIP_QUOTED.sub("''", cleaned)
    if ";" in no_strings:
        return False
    first_token = cleaned.strip().split()[0].upper() if cleaned.strip() else ""
    if first_token not in ("SELECT", "WITH"):
        return False
    if first_token == "WITH" and not re.search(r"\bSELECT\b", cleaned, re.IGNORECASE):
        return False
    if _DANGEROUS_KW.search(no_strings):
        return False
    return True


def _inject_limit(sql: str, max_rows: int) -> str:
    """Ensure sql has a LIMIT no greater than max_rows."""
    sql = sql.rstrip().rstrip(";")
    existing = re.search(r"\bLIMIT\s+(\d+)", sql, re.IGNORECASE)
    if existing:
        n = int(existing.group(1))
        if n > max_rows:
            sql = re.sub(r"\bLIMIT\s+\d+", f"LIMIT {max_rows}", sql, flags=re.IGNORECASE)
    else:
        sql = f"{sql} LIMIT {max_rows}"
    return sql


# ── Parameter handling ────────────────────────────────────────────────────────

def extract_param_names(sql: str) -> list[str]:
    """Return deduplicated list of {{param_name}} placeholders found in sql."""
    seen: dict[str, None] = {}
    for m in _PARAM_PATTERN.finditer(sql):
        seen[m.group(1)] = None
    return list(seen)


def _coerce_param(name: str, value: Any, param_type: str) -> Any:
    """Coerce and validate a param value to the declared type."""
    if param_type == "number":
        try:
            f = float(str(value))
            return int(f) if f == int(f) else f
        except (ValueError, TypeError):
            raise bad_request("PARAM_TYPE_ERROR", f"Parameter '{name}' must be a number")
    if param_type == "date":
        try:
            datetime.strptime(str(value), "%Y-%m-%d")
            return str(value)
        except ValueError:
            raise bad_request(
                "PARAM_TYPE_ERROR", f"Parameter '{name}' must be a date in YYYY-MM-DD format"
            )
    if param_type == "datetime":
        try:
            datetime.fromisoformat(str(value))
            return str(value)
        except ValueError:
            raise bad_request(
                "PARAM_TYPE_ERROR", f"Parameter '{name}' must be a datetime in ISO format"
            )
    return str(value)  # text type


def apply_params(
    sql: str,
    param_defs: list[dict],
    param_values: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Replace {{name}} placeholders with :name bind params and resolve values.

    Uses SQLAlchemy's parameterised execution — never string interpolation.
    Returns (modified_sql, resolved_values_dict).
    """
    param_map = {p["name"]: p for p in (param_defs or [])}

    resolved: dict[str, Any] = {}
    for name, pdef in param_map.items():
        if name in param_values and param_values[name] is not None and str(param_values[name]) != "":
            resolved[name] = _coerce_param(name, param_values[name], pdef.get("type", "text"))
        elif pdef.get("default_value") is not None:
            resolved[name] = _coerce_param(name, pdef["default_value"], pdef.get("type", "text"))
        else:
            raise bad_request("PARAM_MISSING", f"Parameter '{name}' is required but has no value or default")

    # Also handle any {{name}} in SQL that have no definition — treat as text
    for name in extract_param_names(sql):
        if name not in resolved:
            if name in param_values:
                resolved[name] = str(param_values[name])
            else:
                raise bad_request("PARAM_MISSING", f"Value for parameter '{name}' is required")

    modified_sql = _PARAM_PATTERN.sub(lambda m: f":{m.group(1)}", sql)
    return modified_sql, resolved


# ── Serialization ─────────────────────────────────────────────────────────────

def _serialize_value(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, bytes):
        return base64.b64encode(v).decode()
    if isinstance(v, UUID):
        return str(v)
    return v


def _serialize_rows(rows: list, columns: list[str]) -> list[dict[str, Any]]:
    return [
        {col: _serialize_value(val) for col, val in zip(columns, row)}
        for row in rows
    ]


# ── Connection helper ─────────────────────────────────────────────────────────

async def _get_specific_engine(
    db: AsyncSession, org_id: UUID, connection_id: UUID
) -> tuple[AsyncEngine, Connection]:
    """Open an engine for a specific connection, validating org ownership."""
    result = await db.execute(
        select(Connection).where(
            Connection.id == connection_id,
            Connection.org_id == org_id,
            Connection.deleted_at.is_(None),
        )
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise not_found("Connection not found")
    if not conn.encrypted_dsn:
        raise bad_request("CONNECTION_ERROR", "Connection has no DSN configured")
    dsn = decrypt_dsn(conn.encrypted_dsn)
    if parse_pulse_api_payload(dsn) is not None:
        raise bad_request(
            "CONNECTION_ERROR",
            "This connection is not a SQL database. Studio requires a SQL connection.",
        )
    url = sync_dsn_to_async_sqlalchemy_url(dsn)
    engine = create_async_engine(
        url, connect_args=connect_args_for_async_url(url, conn.sslmode)
    )
    return engine, conn


# ── Main execution entry point ────────────────────────────────────────────────

async def execute_studio_query(
    db: AsyncSession,
    org_id: UUID,
    connection_id: UUID | None,
    sql_text: str,
    *,
    param_defs: list[dict] | None = None,
    param_values: dict[str, Any] | None = None,
    page: int = 1,
    page_size: int = 100,
    redis=None,
    org_plan: str = "free",
) -> dict[str, Any]:
    """Execute a user-supplied SELECT query against the org's client DB.

    Security guarantees:
    1. _is_select_only rejects non-SELECT before any DB round-trip
    2. apply_params uses SQLAlchemy bind params (never string interpolation)
    3. safe_client_connection enforces READ ONLY + timeout at session level
    4. _inject_limit caps result set regardless of user's LIMIT clause
    5. Org isolation: connection_id must belong to org_id
    """
    # Enforce daily execution budget for cloud free plan
    from app.api.dependencies.plan_gate import check_studio_execution_budget
    await check_studio_execution_budget(org_id, redis, plan=org_plan)

    if not _is_select_only(sql_text):
        raise bad_request(
            "INVALID_SQL",
            "Only SELECT statements are permitted in Pulse Studio. "
            "INSERT, UPDATE, DELETE, DROP and similar operations are blocked.",
        )

    # Apply LIMIT cap on the template before param substitution
    limited_sql = _inject_limit(sql_text.strip(), _MAX_ROWS)

    # Resolve parameters: {{name}} → :name  +  bound values dict
    bound_values: dict[str, Any] = {}
    if _PARAM_PATTERN.search(limited_sql):
        limited_sql, bound_values = apply_params(
            limited_sql,
            param_defs or [],
            param_values or {},
        )

    # Cache key includes both the SQL template and param values so different
    # param combinations are cached independently.
    params_fingerprint = (
        hashlib.sha256(
            json.dumps(bound_values, sort_keys=True, default=str).encode()
        ).hexdigest()[:8]
        if bound_values
        else ""
    )
    sql_hash = hashlib.sha256(limited_sql.encode()).hexdigest()[:12]
    cache_key = f"{_CACHE_PREFIX}{org_id}:{sql_hash}{params_fingerprint}"

    # --- Redis cache read ---
    if redis is not None:
        try:
            cached_raw = await redis.get(cache_key)
            if cached_raw:
                cached = json.loads(cached_raw)
                all_rows = cached["rows"]
                columns = cached["columns"]
                total = cached["total"]
                start = (page - 1) * page_size
                return {
                    "rows": all_rows[start : start + page_size],
                    "columns": columns,
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                    "cached": True,
                }
        except Exception:
            logger.warning("Studio cache read failed for org=%s", org_id, exc_info=True)

    # --- Execute against client DB ---
    engine: AsyncEngine | None = None
    try:
        if connection_id is not None:
            engine, conn = await _get_specific_engine(db, org_id, connection_id)
        else:
            engine, conn = await open_client_engine(db, org_id)

        async with safe_client_connection(engine, conn) as client_conn:
            stmt = text(limited_sql)
            result = await client_conn.execute(stmt, bound_values)
            raw_rows = result.all()
            columns = list(result.keys())

        rows_serialized = _serialize_rows(raw_rows, columns)
        total = len(rows_serialized)

        # --- Redis cache write ---
        if redis is not None:
            try:
                payload = json.dumps(
                    {"rows": rows_serialized, "columns": columns, "total": total},
                    default=str,
                )
                await redis.set(cache_key, payload, ex=_CACHE_TTL)
            except Exception:
                logger.warning("Studio cache write failed for org=%s", org_id, exc_info=True)

        start = (page - 1) * page_size
        return {
            "rows": rows_serialized[start : start + page_size],
            "columns": columns,
            "total": total,
            "page": page,
            "page_size": page_size,
            "cached": False,
        }

    except ClientDBError as exc:
        raise bad_request("CLIENT_DB_ERROR", str(exc)) from exc
    finally:
        if engine is not None:
            await engine.dispose()
