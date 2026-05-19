"""Shared query tools for autonomous agents.

Security hardened:
- WHERE clause blocklist prevents SQL injection via LLM-generated clauses
- All connections use READ-ONLY mode via _safe_client_connection
- Structured audit logging on every client data access
- Row limits enforced (max 500 per query)
"""

import logging
import re
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.tools.base import Tool, ToolParam
from app.agents.tools.client_db import (
    open_client_engine,
    quote_identifier,
    safe_client_connection,
    schema_columns_sql,
    validate_identifier,
)
from app.infrastructure.database.client_queries import ClientDBError, get_schema_mapping

logger = logging.getLogger(__name__)

# Dedicated audit logger for client data access — separate from application logs
# so it can be routed to compliance/SIEM systems independently.
audit_logger = logging.getLogger("pulse.client_data_audit")

# ─── SQL injection prevention ───────────────────────────────────────────

_DANGEROUS_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE"
    r"|EXEC|EXECUTE|COPY|LOAD|INTO\s+OUTFILE|INTO\s+DUMPFILE)\b",
    re.IGNORECASE,
)


def _coerce_limit(limit: int | str | None) -> int:
    if limit is None:
        return 100
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise ClientDBError(f"Invalid limit: {limit!r}") from exc
    return max(1, min(value, 500))


def _validate_where_clause(where: str | None) -> str | None:
    """Validate a WHERE clause for safety before SQL interpolation.

    Blocks:
    - Semicolons (statement chaining)
    - Dangerous SQL keywords (INSERT, UPDATE, DELETE, DROP, etc.)
    - Comment markers (-- or /*)
    """
    if where is None:
        return None

    # Block statement chaining
    if ";" in where:
        raise ClientDBError("WHERE clause must not contain semicolons")

    # Block SQL comments
    if "--" in where or "/*" in where:
        raise ClientDBError("WHERE clause must not contain SQL comments")

    # Block dangerous keywords
    if _DANGEROUS_SQL.search(where):
        raise ClientDBError(
            "WHERE clause contains forbidden SQL keyword. "
            "Only SELECT-compatible expressions are allowed."
        )

    return where


# ─── Audit logging ──────────────────────────────────────────────────────

async def _fetch_table_columns(
    client_conn: Any, db_type: str, table_name: str
) -> list[str]:
    """Return column names for a table from information_schema."""
    cols_result = await client_conn.execute(
        text(schema_columns_sql(db_type)),
        {"tname": table_name},
    )
    return [row[0] for row in cols_result.all()]


def _log_client_access(
    org_id: UUID,
    table: str,
    operation: str,
    row_count: int,
    columns: str | None = None,
) -> None:
    """Emit a structured audit log entry for every client data access.

    These logs can be collected by SIEM/compliance tooling to answer:
    "What data was accessed, when, for which org, and how many rows?"
    """
    audit_logger.info(
        "CLIENT_DATA_ACCESS org=%s table=%s op=%s rows=%d cols=%s",
        org_id, table, operation, row_count,
        columns if columns else "*",
    )


# ─── Tool implementations ──────────────────────────────────────────────

def build_query_tools(db: AsyncSession, org_id: UUID) -> list[Tool]:
    """Build the set of query tools available to autonomous agents."""

    async def query_entity_table(
        columns: str = "*",
        limit: int | str = 100,
        where: str | None = None,
    ) -> dict[str, Any]:
        """Query the mapped entity table in the client database (READ-ONLY)."""
        limit = _coerce_limit(limit)
        where = _validate_where_clause(where)
        mapping = await get_schema_mapping(db, org_id)
        engine, conn = await open_client_engine(db, org_id)
        try:
            async with safe_client_connection(engine, conn) as client_conn:
                table = validate_identifier(mapping.entity_table, "entity table")
                quoted_table = quote_identifier(table, conn.db_type)

                if not columns or columns.strip() == "*":
                    cols_result = await client_conn.execute(
                        text(schema_columns_sql(conn.db_type)),
                        {"tname": mapping.entity_table},
                    )
                    col_names = [r[0] for r in cols_result.all()]
                else:
                    col_names = [
                        validate_identifier(c.strip(), "column")
                        for c in columns.split(",")
                        if c.strip() and c.strip() != "*"
                    ]

                quoted_cols = [quote_identifier(c, conn.db_type) for c in col_names]
                sql = f"SELECT {', '.join(quoted_cols) or '*'} FROM {quoted_table}"

                params: dict[str, Any] = {}
                if where:
                    sql += f" WHERE {where}"
                sql += " LIMIT :lim"
                params["lim"] = limit

                result = await client_conn.execute(text(sql), params)
                rows = result.all()

                _log_client_access(org_id, table, "query_entity_table", len(rows), columns)

                return {
                    "rows": [dict(zip(col_names, row)) for row in rows],
                    "count": len(rows),
                    "table": table,
                }
        finally:
            await engine.dispose()

    async def query_related_table(
        table_name: str,
        columns: str = "*",
        limit: int | str = 100,
        where: str | None = None,
    ) -> dict[str, Any]:
        """Query any table in the client database for cross-table analysis (READ-ONLY)."""
        limit = _coerce_limit(limit)
        where = _validate_where_clause(where)
        engine, conn = await open_client_engine(db, org_id)
        try:
            async with safe_client_connection(engine, conn) as client_conn:
                table = validate_identifier(table_name, "table")
                quoted_table = quote_identifier(table, conn.db_type)

                available_columns = await _fetch_table_columns(
                    client_conn, conn.db_type, table
                )
                if not available_columns:
                    return {
                        "error": f"Table '{table}' not found",
                        "hint": "Call list_tables and use an exact table name from the result.",
                    }

                if not columns or columns.strip() == "*":
                    col_names = available_columns
                else:
                    col_names = []
                    missing: list[str] = []
                    for raw in columns.split(","):
                        name = raw.strip()
                        if not name or name == "*":
                            continue
                        try:
                            col = validate_identifier(name, "column")
                        except ClientDBError as exc:
                            return {"error": str(exc), "table": table}
                        if col not in available_columns:
                            missing.append(col)
                        else:
                            col_names.append(col)
                    if missing:
                        err: dict[str, Any] = {
                            "error": (
                                f"Column(s) {missing} do not exist on table '{table}'"
                            ),
                            "table": table,
                            "missing_columns": missing,
                            "available_columns": available_columns,
                        }
                        try:
                            mapping = await get_schema_mapping(db, org_id)
                            entity_cols = await _fetch_table_columns(
                                client_conn,
                                conn.db_type,
                                mapping.entity_table,
                            )
                            if any(c in entity_cols for c in missing):
                                err["hint"] = (
                                    f"Column(s) exist on entity table "
                                    f"'{mapping.entity_table}'; use "
                                    "query_entity_table instead."
                                )
                        except Exception:
                            pass
                        return err
                    if not col_names:
                        return {
                            "error": "No valid columns specified",
                            "table": table,
                            "available_columns": available_columns,
                        }

                quoted_cols = [quote_identifier(c, conn.db_type) for c in col_names]
                sql = f"SELECT {', '.join(quoted_cols) or '*'} FROM {quoted_table}"

                params: dict[str, Any] = {}
                if where:
                    sql += f" WHERE {where}"
                sql += " LIMIT :lim"
                params["lim"] = limit

                result = await client_conn.execute(text(sql), params)
                rows = result.all()

                _log_client_access(org_id, table, "query_related_table", len(rows), columns)

                return {
                    "rows": [dict(zip(col_names, row)) for row in rows],
                    "count": len(rows),
                    "table": table,
                }
        finally:
            await engine.dispose()

    async def query_aggregate(
        table_name: str,
        aggregate: str,
        column: str,
        group_by: str | None = None,
        where: str | None = None,
    ) -> dict[str, Any]:
        """Run an aggregate query (COUNT, SUM, AVG, MIN, MAX) on the client database (READ-ONLY)."""
        where = _validate_where_clause(where)
        engine, conn = await open_client_engine(db, org_id)
        try:
            async with safe_client_connection(engine, conn) as client_conn:
                table = validate_identifier(table_name, "table")
                quoted_table = quote_identifier(table, conn.db_type)

                agg = aggregate.upper()
                if agg not in ("COUNT", "SUM", "AVG", "MIN", "MAX"):
                    return {"error": f"Unsupported aggregate: {aggregate}"}

                available_columns = await _fetch_table_columns(
                    client_conn, conn.db_type, table
                )
                if not available_columns:
                    return {
                        "error": f"Table '{table}' not found or has no columns",
                        "table": table,
                    }

                if column != "*":
                    col_name = column.strip()
                    try:
                        col = validate_identifier(col_name, "column")
                    except ClientDBError as exc:
                        return {"error": str(exc), "table": table}

                    if col not in available_columns:
                        err: dict[str, Any] = {
                            "error": (
                                f"Column '{col}' does not exist on table '{table}'"
                            ),
                            "table": table,
                            "column": col,
                            "available_columns": available_columns,
                        }
                        try:
                            mapping = await get_schema_mapping(db, org_id)
                            entity_cols = await _fetch_table_columns(
                                client_conn,
                                conn.db_type,
                                mapping.entity_table,
                            )
                            if col in entity_cols:
                                err["hint"] = (
                                    f"Column '{col}' is on entity table "
                                    f"'{mapping.entity_table}'; use query_entity_table "
                                    f"or query_aggregate on that table."
                                )
                        except Exception:
                            pass
                        return err

                    agg_expr = f"{agg}({quote_identifier(col, conn.db_type)})"
                else:
                    if agg != "COUNT":
                        return {
                            "error": "Only COUNT supports column '*'",
                            "table": table,
                        }
                    agg_expr = f"{agg}(*)"

                sql = f"SELECT {agg_expr} AS result"

                select_cols = ["result"]
                if group_by:
                    gb_col = validate_identifier(group_by, "group_by column")
                    if gb_col not in available_columns:
                        return {
                            "error": (
                                f"GROUP BY column '{gb_col}' does not exist on "
                                f"table '{table}'"
                            ),
                            "table": table,
                            "available_columns": available_columns,
                        }
                    quoted_gb = quote_identifier(gb_col, conn.db_type)
                    sql = f"SELECT {quoted_gb}, {agg_expr} AS result"
                    select_cols = [gb_col, "result"]

                sql += f" FROM {quoted_table}"

                params: dict[str, Any] = {}
                if where:
                    sql += f" WHERE {where}"
                if group_by:
                    sql += f" GROUP BY {quoted_gb} ORDER BY result DESC LIMIT 50"

                result = await client_conn.execute(text(sql), params)
                rows = result.all()

                _log_client_access(org_id, table, f"query_aggregate({agg})", len(rows))

                return {
                    "results": [dict(zip(select_cols, row)) for row in rows],
                    "aggregate": agg,
                    "column": column,
                    "table": table,
                }
        finally:
            await engine.dispose()

    async def list_tables() -> dict[str, Any]:
        """List all tables in the client database with their columns (READ-ONLY)."""
        engine, conn = await open_client_engine(db, org_id)
        try:
            async with safe_client_connection(engine, conn) as client_conn:
                if conn.db_type == "mysql":
                    tables_sql = (
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = DATABASE() ORDER BY table_name"
                    )
                else:
                    tables_sql = (
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' ORDER BY table_name"
                    )
                tables_result = await client_conn.execute(text(tables_sql))
                table_names = [row[0] for row in tables_result.all()]

                tables = []
                for tname in table_names:
                    cols_result = await client_conn.execute(
                        text(schema_columns_sql(conn.db_type)),
                        {"tname": tname},
                    )
                    columns = [row[0] for row in cols_result.all()]
                    tables.append({"table": tname, "columns": columns})

                _log_client_access(org_id, "information_schema", "list_tables", len(tables))

                return {"tables": tables, "count": len(tables)}
        finally:
            await engine.dispose()

    async def get_row_count(table_name: str) -> dict[str, Any]:
        """Get row count of a table in the client database (READ-ONLY)."""
        engine, conn = await open_client_engine(db, org_id)
        try:
            async with safe_client_connection(engine, conn) as client_conn:
                table = validate_identifier(table_name, "table")
                quoted = quote_identifier(table, conn.db_type)
                result = await client_conn.execute(text(f"SELECT COUNT(*) FROM {quoted}"))
                count = result.scalar_one()

                _log_client_access(org_id, table, "get_row_count", 1)

                return {"table": table, "row_count": count}
        finally:
            await engine.dispose()

    async def validate_column_exists(
        table_name: str, column_name: str
    ) -> dict[str, Any]:
        """Check if a column exists in a table (READ-ONLY)."""
        engine, conn = await open_client_engine(db, org_id)
        try:
            async with safe_client_connection(engine, conn) as client_conn:
                cols_result = await client_conn.execute(
                    text(schema_columns_sql(conn.db_type)),
                    {"tname": table_name},
                )
                all_cols = [row[0] for row in cols_result.all()]
                exists = column_name in all_cols

                _log_client_access(org_id, table_name, "validate_column_exists", 1)

                return {
                    "table": table_name,
                    "column": column_name,
                    "exists": exists,
                    "available_columns": all_cols,
                }
        finally:
            await engine.dispose()

    # Build the tool list
    return [
        Tool(
            name="query_entity_table",
            description=(
                "Read rows from the org's mapped entity table (READ-ONLY transaction). "
                "Returns: {rows: list[dict], count: int, table: str}. "
                "Limits: max 500 rows per call; WHERE clauses are SELECT-only (no DML/DDL/comments/semicolons)."
            ),
            parameters=[
                ToolParam("columns", "string", "Comma-separated column names or '*' for all", required=False),
                ToolParam("limit", "string", "Max rows to return (numeric string, capped at 500)", required=False),
                ToolParam("where", "string", "SQL WHERE clause (SELECT-only, no INSERT/UPDATE/DELETE)", required=False),
            ],
            execute=query_entity_table,
        ),
        Tool(
            name="query_related_table",
            description=(
                "Read rows from any table in the client database for cross-table analysis (READ-ONLY transaction). "
                "Returns: {rows: list[dict], count: int, table: str} on success, or {error: str} if the table is not found. "
                "Limits: max 500 rows per call; WHERE clauses are SELECT-only."
            ),
            parameters=[
                ToolParam("table_name", "string", "Name of the table to query"),
                ToolParam("columns", "string", "Comma-separated column names or '*'", required=False),
                ToolParam("limit", "string", "Max rows to return (numeric string, capped at 500)", required=False),
                ToolParam("where", "string", "SQL WHERE clause (SELECT-only, no INSERT/UPDATE/DELETE)", required=False),
            ],
            execute=query_related_table,
        ),
        Tool(
            name="query_aggregate",
            description=(
                "Run a single aggregate (COUNT/SUM/AVG/MIN/MAX) on any table, optionally grouped (READ-ONLY). "
                "Returns: {results: list[dict], aggregate: str, column: str, table: str}, or {error: str} for an unsupported aggregate. "
                "Limits: GROUP BY results capped at 50 rows (ordered by result DESC); WHERE clauses are SELECT-only."
            ),
            parameters=[
                ToolParam("table_name", "string", "Table to aggregate"),
                ToolParam("aggregate", "string", "Aggregate function: COUNT, SUM, AVG, MIN, MAX"),
                ToolParam("column", "string", "Column to aggregate (or '*' for COUNT)"),
                ToolParam("group_by", "string", "Column to group by", required=False),
                ToolParam("where", "string", "SQL WHERE clause (SELECT-only, no INSERT/UPDATE/DELETE)", required=False),
            ],
            execute=query_aggregate,
        ),
        Tool(
            name="list_tables",
            description=(
                "List every table in the client database with its column names (READ-ONLY). "
                "Returns: {tables: list[{name: str, columns: list[str]}], count: int}. "
                "Use this once at the start of an exploration; do not call repeatedly."
            ),
            parameters=[],
            execute=list_tables,
        ),
        Tool(
            name="get_row_count",
            description=(
                "Count all rows in a single table (READ-ONLY). "
                "Returns: {table: str, row_count: int}. "
                "May be slow on very large unindexed tables; do not use as a hot loop."
            ),
            parameters=[
                ToolParam("table_name", "string", "Name of the table"),
            ],
            execute=get_row_count,
        ),
        Tool(
            name="validate_column_exists",
            description=(
                "Check whether a specific column is present in a table (READ-ONLY). "
                "Returns: {exists: bool, available_columns: list[str]}. "
                "Use before referencing an unverified column in another tool call."
            ),
            parameters=[
                ToolParam("table_name", "string", "Table to check"),
                ToolParam("column_name", "string", "Column name to validate"),
            ],
            execute=validate_column_exists,
        ),
    ]
