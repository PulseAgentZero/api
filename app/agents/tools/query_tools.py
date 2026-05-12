"""Shared query tools for autonomous agents.

These wrap the existing client_queries engine so agents can fire live
queries against the org's external database via tool calls.
"""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.tools.base import Tool, ToolParam
from app.infrastructure.database.client_queries import (
    _get_client_engine,
    _quote_identifier,
    _schema_columns_sql,
    _validate_identifier,
    get_schema_mapping,
    fetch_entities,
    fetch_entity_by_id,
    compute_risk,
)

logger = logging.getLogger(__name__)


def build_query_tools(db: AsyncSession, org_id: UUID) -> list[Tool]:
    """Build the set of query tools available to autonomous agents."""

    async def query_entity_table(
        columns: str = "*",
        limit: int = 100,
        where: str | None = None,
    ) -> dict[str, Any]:
        """Query the mapped entity table in the client database."""
        mapping = await get_schema_mapping(db, org_id)
        engine, conn = await _get_client_engine(db, org_id)
        try:
            async with engine.connect() as client_conn:
                table = _validate_identifier(mapping.entity_table, "entity table")
                quoted_table = _quote_identifier(table, conn.db_type)

                if columns == "*":
                    cols_result = await client_conn.execute(
                        text(_schema_columns_sql(conn.db_type)),
                        {"tname": mapping.entity_table},
                    )
                    col_names = [r[0] for r in cols_result.all()]
                else:
                    col_names = [
                        _validate_identifier(c.strip(), "column")
                        for c in columns.split(",")
                    ]

                quoted_cols = [_quote_identifier(c, conn.db_type) for c in col_names]
                sql = f"SELECT {', '.join(quoted_cols)} FROM {quoted_table}"

                params: dict[str, Any] = {}
                if where:
                    sql += f" WHERE {where}"
                sql += " LIMIT :lim"
                params["lim"] = min(limit, 500)

                result = await client_conn.execute(text(sql), params)
                rows = result.all()
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
        limit: int = 100,
        where: str | None = None,
    ) -> dict[str, Any]:
        """Query any table in the client database (for cross-table analysis)."""
        engine, conn = await _get_client_engine(db, org_id)
        try:
            async with engine.connect() as client_conn:
                table = _validate_identifier(table_name, "table")
                quoted_table = _quote_identifier(table, conn.db_type)

                if columns == "*":
                    cols_result = await client_conn.execute(
                        text(_schema_columns_sql(conn.db_type)),
                        {"tname": table_name},
                    )
                    col_names = [r[0] for r in cols_result.all()]
                    if not col_names:
                        return {"error": f"Table '{table_name}' not found"}
                else:
                    col_names = [
                        _validate_identifier(c.strip(), "column")
                        for c in columns.split(",")
                    ]

                quoted_cols = [_quote_identifier(c, conn.db_type) for c in col_names]
                sql = f"SELECT {', '.join(quoted_cols)} FROM {quoted_table}"

                params: dict[str, Any] = {}
                if where:
                    sql += f" WHERE {where}"
                sql += " LIMIT :lim"
                params["lim"] = min(limit, 500)

                result = await client_conn.execute(text(sql), params)
                rows = result.all()
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
        """Run an aggregate query (COUNT, SUM, AVG, MIN, MAX) on the client database."""
        engine, conn = await _get_client_engine(db, org_id)
        try:
            async with engine.connect() as client_conn:
                table = _validate_identifier(table_name, "table")
                quoted_table = _quote_identifier(table, conn.db_type)

                agg = aggregate.upper()
                if agg not in ("COUNT", "SUM", "AVG", "MIN", "MAX"):
                    return {"error": f"Unsupported aggregate: {aggregate}"}

                if column == "*":
                    agg_expr = f"{agg}(*)"
                else:
                    col = _validate_identifier(column, "column")
                    agg_expr = f"{agg}({_quote_identifier(col, conn.db_type)})"

                sql = f"SELECT {agg_expr} AS result"

                select_cols = ["result"]
                if group_by:
                    gb_col = _validate_identifier(group_by, "group_by column")
                    quoted_gb = _quote_identifier(gb_col, conn.db_type)
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
                return {
                    "results": [dict(zip(select_cols, row)) for row in rows],
                    "aggregate": agg,
                    "column": column,
                    "table": table,
                }
        finally:
            await engine.dispose()

    async def list_tables() -> dict[str, Any]:
        """List all tables in the client database with their columns."""
        engine, conn = await _get_client_engine(db, org_id)
        try:
            async with engine.connect() as client_conn:
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
                        text(_schema_columns_sql(conn.db_type)),
                        {"tname": tname},
                    )
                    columns = [row[0] for row in cols_result.all()]
                    tables.append({"table": tname, "columns": columns})
                return {"tables": tables, "count": len(tables)}
        finally:
            await engine.dispose()

    async def get_row_count(table_name: str) -> dict[str, Any]:
        """Get row count of a table in the client database."""
        engine, conn = await _get_client_engine(db, org_id)
        try:
            async with engine.connect() as client_conn:
                table = _validate_identifier(table_name, "table")
                quoted = _quote_identifier(table, conn.db_type)
                result = await client_conn.execute(text(f"SELECT COUNT(*) FROM {quoted}"))
                count = result.scalar_one()
                return {"table": table, "row_count": count}
        finally:
            await engine.dispose()

    async def validate_column_exists(
        table_name: str, column_name: str
    ) -> dict[str, Any]:
        """Check if a column exists in a table."""
        engine, conn = await _get_client_engine(db, org_id)
        try:
            async with engine.connect() as client_conn:
                cols_result = await client_conn.execute(
                    text(_schema_columns_sql(conn.db_type)),
                    {"tname": table_name},
                )
                all_cols = [row[0] for row in cols_result.all()]
                exists = column_name in all_cols
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
            description="Query the mapped entity table. Returns rows from the org's primary entity table.",
            parameters=[
                ToolParam("columns", "string", "Comma-separated column names or '*' for all", required=False),
                ToolParam("limit", "integer", "Max rows to return (max 500)", required=False),
                ToolParam("where", "string", "SQL WHERE clause (use parameterized values)", required=False),
            ],
            execute=query_entity_table,
        ),
        Tool(
            name="query_related_table",
            description="Query any table in the client database for cross-table analysis.",
            parameters=[
                ToolParam("table_name", "string", "Name of the table to query"),
                ToolParam("columns", "string", "Comma-separated column names or '*'", required=False),
                ToolParam("limit", "integer", "Max rows to return (max 500)", required=False),
                ToolParam("where", "string", "SQL WHERE clause", required=False),
            ],
            execute=query_related_table,
        ),
        Tool(
            name="query_aggregate",
            description="Run an aggregate query (COUNT, SUM, AVG, MIN, MAX) on any table.",
            parameters=[
                ToolParam("table_name", "string", "Table to aggregate"),
                ToolParam("aggregate", "string", "Aggregate function: COUNT, SUM, AVG, MIN, MAX"),
                ToolParam("column", "string", "Column to aggregate (or '*' for COUNT)"),
                ToolParam("group_by", "string", "Column to group by", required=False),
                ToolParam("where", "string", "SQL WHERE clause", required=False),
            ],
            execute=query_aggregate,
        ),
        Tool(
            name="list_tables",
            description="List all tables in the client database with their column names.",
            parameters=[],
            execute=list_tables,
        ),
        Tool(
            name="get_row_count",
            description="Get the total row count of a table.",
            parameters=[
                ToolParam("table_name", "string", "Name of the table"),
            ],
            execute=get_row_count,
        ),
        Tool(
            name="validate_column_exists",
            description="Check if a specific column exists in a table.",
            parameters=[
                ToolParam("table_name", "string", "Table to check"),
                ToolParam("column_name", "string", "Column name to validate"),
            ],
            execute=validate_column_exists,
        ),
    ]
