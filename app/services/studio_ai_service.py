"""Pulse Studio — AI-powered query generation and visualization recommendation."""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import bad_request, not_found
from app.infrastructure.llm.factory import get_llm_client

logger = logging.getLogger(__name__)

_CHART_TYPES = {
    "bar", "line", "area", "pie", "scatter", "table", "number",
    "funnel", "heatmap", "gauge", "waterfall", "trend",
}


def _get_llm():
    client = get_llm_client()
    if not client.is_configured():
        raise bad_request(
            "AI_NOT_CONFIGURED",
            f"AI provider '{client.provider_name}' is not configured. "
            "Set AI_PROVIDER and the corresponding API key.",
        )
    return client


async def _introspect_schema(
    db: AsyncSession, org_id: UUID, connection_id: UUID | None
) -> str:
    """Return a compact schema description string for LLM context."""
    from sqlalchemy import text as _text

    from app.agents.tools.client_db import (
        open_client_engine,
        safe_client_connection,
        schema_columns_sql,
    )
    from app.infrastructure.database.client_queries import ClientDBError
    from app.services.studio_query_service import _get_specific_engine

    try:
        if connection_id is not None:
            engine, conn = await _get_specific_engine(db, org_id, connection_id)
        else:
            engine, conn = await open_client_engine(db, org_id)
        try:
            db_type = getattr(conn, "db_type", None)
            if db_type == "mysql":
                tables_sql = "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE() LIMIT 20"
            elif db_type == "mssql":
                tables_sql = "SELECT TOP 20 TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE='BASE TABLE'"
            elif db_type == "sqlite":
                tables_sql = "SELECT name FROM sqlite_master WHERE type='table' LIMIT 20"
            else:
                tables_sql = (
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE' LIMIT 20"
                )
            cols_sql = schema_columns_sql(db_type)
            async with safe_client_connection(engine, conn) as client_conn:
                table_rows = (await client_conn.execute(_text(tables_sql))).all()
                table_names = [r[0] for r in table_rows]
                lines = []
                for tname in table_names[:20]:
                    try:
                        col_rows = (
                            await client_conn.execute(_text(cols_sql), {"tname": tname})
                        ).all()
                        cols = [r[0] for r in col_rows[:20]]
                        lines.append(f"table: {tname} | columns: {', '.join(cols)}")
                    except Exception:
                        lines.append(f"table: {tname}")
            return "\n".join(lines)
        finally:
            await engine.dispose()
    except ClientDBError as exc:
        raise bad_request("CLIENT_DB_ERROR", str(exc)) from exc


async def generate_sql_from_goal(
    db: AsyncSession,
    org_id: UUID,
    goal: str,
    connection_id: UUID | None,
) -> dict[str, Any]:
    """Generate a SELECT query from a natural language goal.

    Returns {"sql": str, "explanation": str, "params": list[dict]}.
    Does NOT save anything — user reviews and saves manually.
    """
    from app.services.studio_query_service import _is_select_only

    schema_context = await _introspect_schema(db, org_id, connection_id)
    if not schema_context:
        raise bad_request("NO_SCHEMA", "No tables found in the connected database")

    llm = _get_llm()
    system = (
        "You are a SQL expert. Given a database schema and a goal, return a JSON object with:\n"
        '  "sql": a single SELECT statement. Use {{param_name}} for any user-configurable values.\n'
        '  "explanation": 1-2 sentences in plain English describing what the query does.\n'
        '  "params": array of {name, type, default_value, label} for each {{placeholder}}.\n'
        "   types are: text, number, date, datetime.\n"
        "Output ONLY valid JSON. No markdown."
    )
    user_msg = f"Schema:\n{schema_context}\n\nGoal: {goal}"

    raw = await llm.complete(system, user_msg, max_tokens=1500, temperature=0.1)
    try:
        result = json.loads(raw)
        sql = str(result.get("sql", ""))
        if not _is_select_only(sql):
            raise bad_request("INVALID_AI_SQL", "Generated SQL was not a safe SELECT statement")
        return {
            "sql": sql,
            "explanation": str(result.get("explanation", "")),
            "params": result.get("params") or [],
        }
    except (json.JSONDecodeError, KeyError) as exc:
        logger.warning("[studio_ai] generate_sql_from_goal parse failed: %s", exc)
        raise bad_request("AI_PARSE_ERROR", "Could not parse AI response — please try again") from exc


def _classify_columns(rows: list[dict], columns: list[str]) -> dict[str, str]:
    """Classify each column as 'numeric', 'date', or 'categorical' using sample data."""
    from datetime import date, datetime
    from decimal import Decimal

    types: dict[str, str] = {}
    for col in columns:
        sample_vals = [r[col] for r in rows[:10] if r.get(col) is not None]
        if not sample_vals:
            types[col] = "categorical"
            continue
        if all(isinstance(v, (int, float, Decimal)) for v in sample_vals):
            types[col] = "numeric"
        elif all(isinstance(v, (datetime, date)) for v in sample_vals):
            types[col] = "date"
        else:
            # Try parsing strings as dates
            try:
                from datetime import datetime as _dt
                [_dt.fromisoformat(str(v)) for v in sample_vals[:3]]
                types[col] = "date"
            except (ValueError, TypeError):
                types[col] = "categorical"
    return types


async def recommend_visualization(
    db: AsyncSession,
    org_id: UUID,
    query_id: UUID,
    redis=None,
) -> dict[str, Any]:
    """Recommend a chart type and config for a saved query.

    Returns {"chart_type": str, "config": dict, "reasoning": str}.
    """
    import json as _json

    from app.infrastructure.database.repositories.studio_query_repository import StudioQueryRepository
    from app.infrastructure.database.repositories.studio_query_run_repository import StudioQueryRunRepository
    from app.infrastructure.redis.keys import studio_run_result
    from app.services.studio_query_service import execute_studio_query

    q = await StudioQueryRepository(db).get_by_id_and_org(query_id, org_id)
    if not q:
        raise not_found("Query not found")

    # Try to get cached result from most recent completed run
    rows: list[dict] = []
    columns: list[str] = []
    cached = False

    if redis is not None:
        runs = await StudioQueryRunRepository(db).list_by_query(query_id, org_id, limit=1)
        if runs and runs[0].status == "completed":
            try:
                raw = await redis.get(studio_run_result(str(runs[0].id)))
                if raw:
                    data = _json.loads(raw)
                    rows = data.get("rows", [])
                    columns = data.get("columns", [])
                    cached = True
            except Exception:
                pass

    if not cached:
        result = await execute_studio_query(
            db, org_id, q.connection_id, q.sql_text,
            param_defs=q.params or [], param_values={},
            page=1, page_size=100, redis=redis,
        )
        rows = result["rows"]
        columns = result["columns"]

    col_types = _classify_columns(rows, columns)
    numeric_cols = [c for c, t in col_types.items() if t == "numeric"]
    date_cols = [c for c, t in col_types.items() if t == "date"]
    cat_cols = [c for c, t in col_types.items() if t == "categorical"]
    row_count = len(rows)

    col_summary = ", ".join(f"{c}({t})" for c, t in col_types.items())
    llm = _get_llm()
    system = (
        "You are a data visualization expert. Given column types and a row count, recommend "
        "the best chart type and axis configuration. Available chart types: "
        "bar, line, area, pie, scatter, table, number, funnel, heatmap, gauge, waterfall, trend.\n"
        'Return JSON: {"chart_type": "...", "config": {"x_axis": "...", "y_axis": "...", '
        '"title": "..."}, "reasoning": "..."}. No markdown.'
    )
    user_msg = (
        f"Columns: {col_summary}\n"
        f"Row count: {row_count}\n"
        f"Numeric columns: {numeric_cols}\n"
        f"Date columns: {date_cols}\n"
        f"Categorical columns: {cat_cols}"
    )

    raw = await llm.complete(system, user_msg, max_tokens=400, model=llm.fast_model)
    try:
        result = json.loads(raw)
        chart_type = result.get("chart_type", "table")
        if chart_type not in _CHART_TYPES:
            chart_type = "table"
        return {
            "chart_type": chart_type,
            "config": result.get("config") or {},
            "reasoning": str(result.get("reasoning", "")),
        }
    except (json.JSONDecodeError, KeyError):
        return {"chart_type": "table", "config": {}, "reasoning": "Could not determine best chart type"}


async def explain_query(sql_text: str) -> str:
    """Return a plain English explanation of a SQL query."""
    llm = _get_llm()
    return await llm.complete(
        "You are a SQL expert. Explain the following SQL query in plain English "
        "for a non-technical business user. Be concise — 2 to 4 sentences.",
        sql_text,
        max_tokens=300,
        model=llm.fast_model,
    )
