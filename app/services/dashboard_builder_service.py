"""dashboard builder: intake -> plan (preview) -> confirm -> build.

Returns frontend-shaped artifacts (dashboard/src/lib/api/agent-api.ts) so the
agent chat renders structured cards instead of dumping JSON. Only build/apply write.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.infrastructure.database.repositories.connection_repository import ConnectionRepository
from app.services.studio_ai_service import _introspect_schema

logger = logging.getLogger(__name__)

# Intake questions in the frontend's IntakeQuestion shape (prompt/type, not question/choice).
_INTAKE_QUESTION_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "dashboard_name",
        "prompt": "What would you like to name this dashboard?",
        "type": "text",
        "required": True,
        "placeholder": "e.g. Churn Watch",
    },
    {
        "id": "business_goal",
        "prompt": "What business question should this dashboard answer? Be specific: the metric, audience, and decision it supports.",
        "type": "long_text",
        "required": True,
    },
    {
        "id": "success_metric",
        "prompt": "What is the primary success metric or KPI you want to track?",
        "type": "text",
        "required": True,
    },
    {
        "id": "time_window",
        "prompt": "What time window should charts default to? (e.g. last 30 days, last quarter, year-to-date)",
        "type": "text",
        "required": True,
    },
    {
        "id": "segments",
        "prompt": "How should the data be broken down or segmented? (e.g. by region, product, tier, channel)",
        "type": "text",
        "required": False,
    },
    {
        "id": "filters_to_parameterize",
        "prompt": "Which filters should users control on the dashboard? (e.g. date range, region, status — these become dropdowns)",
        "type": "text",
        "required": False,
    },
    {
        "id": "compare_period",
        "prompt": "Do you want a comparison period? (e.g. vs prior month, vs prior year)",
        "type": "text",
        "required": False,
    },
]


def _extract_json_object(raw_text: str) -> dict:
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Expected JSON object")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object")
    return data


def _resolve_date_default(expr: Any) -> str | None:
    """Best-effort: turn a SQL-ish date default into a literal YYYY-MM-DD, else None.

    Studio binds params as values (not SQL), so defaults like CURRENT_DATE or
    date_trunc('year', CURRENT_DATE) never execute — they must be date literals.
    """
    if expr is None:
        return None
    s = str(expr).strip().strip(";")
    if not s:
        return None
    try:  # already a literal date / ISO datetime
        datetime.strptime(s[:10], "%Y-%m-%d")
        return s[:10]
    except ValueError:
        pass
    low = s.lower()
    today = date.today()
    if "date_trunc('year'" in low or 'date_trunc("year"' in low:
        return today.replace(month=1, day=1).isoformat()
    if "date_trunc('month'" in low or 'date_trunc("month"' in low:
        return today.replace(day=1).isoformat()
    m = re.search(r"interval\s+'?\s*(\d+)\s*(day|week|month|year)s?", low)
    if m and any(k in low for k in ("current_date", "now()", "current_timestamp")):
        n, unit = int(m.group(1)), m.group(2)
        if unit == "day":
            return (today - timedelta(days=n)).isoformat()
        if unit == "week":
            return (today - timedelta(weeks=n)).isoformat()
        if unit == "month":
            return (today - timedelta(days=30 * n)).isoformat()
        if unit == "year":
            return today.replace(year=today.year - n).isoformat()
    if low in ("current_date", "now()", "current_timestamp", "current_timestamp()", "today"):
        return today.isoformat()
    return None


def _normalize_param(p: dict) -> dict:
    """Make a param's type/default safe for the Studio executor (mutates in place).

    __time_from/__time_to are injected by the dashboard time range at runtime as ISO
    datetimes, so they must be datetime-typed with no literal default. Other date
    params get SQL-expression defaults resolved to YYYY-MM-DD literals (or dropped).
    """
    if not isinstance(p, dict):
        return p
    name = p.get("name")
    ptype = (p.get("type") or "text").lower()
    if name in ("__time_from", "__time_to"):
        # text (not datetime): the dashboard injects an ISO string at runtime and
        # asyncpg only accepts native datetime objects for date/timestamp params, so
        # we bind these as text and parse them in SQL (see _normalize_time_param_sql).
        p["type"] = "text"
        p["default"] = None
        p["default_value"] = None
        return p
    if ptype in ("date", "datetime"):
        dv = p.get("default_value", p.get("default"))
        resolved = _resolve_date_default(dv)
        p["default"] = resolved
        p["default_value"] = resolved
        return p
    # Filter params (text/string) must have a value or the chart can't render with
    # defaults. The generated SQL guards with "{{p}} = 'ALL' OR col = {{p}}", so the
    # 'ALL' sentinel means "no filter".
    dv = p.get("default_value", p.get("default"))
    if (dv is None or str(dv).strip() == "") and ptype in ("text", "string", ""):
        p["default"] = "ALL"
        p["default_value"] = "ALL"
    return p


def _normalize_params(params: list[dict]) -> list[dict]:
    return [_normalize_param(p) for p in (params or []) if isinstance(p, dict)]


# Postgres `expr::type` casts collide with SQLAlchemy's :name bind parsing
# (`:param::type` makes the bind unrecognized → a literal ':' reaches the DB).
# Rewrite simple casts to CAST(expr AS type), which binds cleanly.
_DOUBLE_COLON_CAST_RE = re.compile(
    r'(\{\{[A-Za-z_]\w*\}\}|"[^"]+"|[A-Za-z_][\w.]*)\s*::\s*([A-Za-z_]\w*)'
)


def _rewrite_double_colon_casts(sql: str) -> str:
    if not sql or "::" not in sql:
        return sql
    prev = None
    out = sql
    while prev != out:  # resolve chained casts (a::int::text)
        prev = out
        out = _DOUBLE_COLON_CAST_RE.sub(r"CAST(\1 AS \2)", out)
    return out


_TIME_PARAM_NAMES = ("__time_from", "__time_to")


def _normalize_time_param_sql(sql: str) -> str:
    """Force {{__time_from/to}} into a text-bound, date-parsed form.

    The dashboard injects these as ISO strings; asyncpg rejects strings for
    date/timestamp params. Binding them inside LEFT(...) keeps them text so the
    string is accepted, then CAST(... AS DATE) parses the YYYY-MM-DD prefix.
    Idempotent: an already-wrapped token is left alone.
    """
    if not sql:
        return sql
    out = sql
    for name in _TIME_PARAM_NAMES:
        tok = "{{%s}}" % name
        if tok not in out:
            continue
        wrapped = "CAST(LEFT(%s, 10) AS DATE)" % tok
        if wrapped in out:
            continue  # already normalized
        # Drop any LLM-applied cast around the bare token first.
        out = re.sub(r"CAST\(\s*" + re.escape(tok) + r"\s+AS\s+\w+\s*\)", tok, out, flags=re.I)
        # str.replace does not re-scan its replacement, so the inner token survives
        # for apply_params to bind — no infinite expansion.
        out = out.replace(tok, wrapped)
    return out


def _clean_chart_sql(sql: str) -> str:
    """All deterministic SQL fixes applied to generated chart SQL before it runs."""
    return _normalize_time_param_sql(_rewrite_double_colon_casts(str(sql or "").strip()))


def _salvage_plan(raw_text: str) -> dict | None:
    """Recover a plan from truncated/partial LLM JSON by keeping complete charts.

    LLMs occasionally exceed the token budget mid-array; rather than failing the
    whole draft, build with the chart objects that did come through intact.
    """
    name_m = re.search(r'"dashboard_name"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_text)
    charts_at = raw_text.find('"charts"')
    desc_m = re.search(r'"description"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_text[:charts_at] if charts_at != -1 else raw_text)
    if charts_at == -1:
        return None
    arr_at = raw_text.find("[", charts_at)
    if arr_at == -1:
        return None

    charts: list[dict] = []
    i, n = arr_at + 1, len(raw_text)
    while i < n:
        while i < n and raw_text[i] not in "{]":
            i += 1
        if i >= n or raw_text[i] == "]":
            break
        depth, in_str, esc, obj_start = 0, False, False, i
        closed = False
        while i < n:
            ch = raw_text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        charts.append(json.loads(raw_text[obj_start : i + 1]))
                    except Exception:
                        pass
                    i += 1
                    closed = True
                    break
            i += 1
        if not closed:
            break  # truncated mid-object — stop here
    if not charts:
        return None
    return {
        "dashboard_name": name_m.group(1) if name_m else None,
        "description": desc_m.group(1) if desc_m else "",
        "charts": charts,
    }


def _build_intake_questions(connections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    questions = [dict(q) for q in _INTAKE_QUESTION_TEMPLATES]
    if len(connections) > 1:
        # Frontend renders the connection picker from artifact.connections, so the
        # question itself just needs id="connection_id".
        questions.insert(
            1,
            {
                "id": "connection_id",
                "prompt": "Which data connection should this dashboard use?",
                "type": "radio",
                "required": True,
            },
        )
    return questions


def _time_range_from_window(time_window: str | None) -> dict[str, Any] | None:
    if not time_window:
        return None
    lower = time_window.lower()
    if "15" in lower and "min" in lower:
        return {"preset": "last_15m"}
    if "1 hour" in lower or "last hour" in lower:
        return {"preset": "last_1h"}
    if "6 hour" in lower:
        return {"preset": "last_6h"}
    if "7 day" in lower or "week" in lower:
        return {"preset": "last_7d"}
    if "30 day" in lower or "month" in lower:
        return {"preset": "last_30d"}
    return {"preset": "last_30d"}


def _aggregate_dashboard_params(charts: list[dict]) -> list[dict[str, Any]]:
    """Collect per-chart {{params}} into dashboard-level filters.

    Each param carries both `default` (frontend agent card) and `default_value`
    (Studio storage) so the same dict serves preview and persistence.
    """
    seen: dict[str, dict] = {}
    for chart in charts:
        for p in chart.get("params") or []:
            if not isinstance(p, dict):
                continue
            name = p.get("name")
            if name and name not in seen:
                default = p.get("default_value", p.get("default"))
                seen[name] = {
                    "name": name,
                    "type": p.get("type", "text"),
                    "default": default,
                    "default_value": default,
                    "label": p.get("label", name.replace("_", " ").title()),
                }
    params = [_normalize_param(p) for p in seen.values()]
    # These are dashboard-level (UI) filters: render time bounds as date pickers,
    # even though the chart query params bind them as text (execution concern).
    for p in params:
        if p.get("name") in ("__time_from", "__time_to"):
            p["type"] = "datetime"
    if params and not any(p["name"] in ("__time_from", "__time_to") for p in params):
        params.extend([
            {"name": "__time_from", "type": "datetime", "label": "From", "default": None, "default_value": None},
            {"name": "__time_to", "type": "datetime", "label": "To", "default": None, "default_value": None},
        ])
    return params


_FILE_SOURCE_CONNECTORS = frozenset({"csv", "excel", "google_sheets", "s3", "gcs"})


def _normalize_dialect(connector_type: str | None) -> str:
    """Map connector_type to the SQL dialect the LLM should target."""
    ct = (connector_type or "").lower().strip()
    if not ct:
        return "postgresql"
    if ct in _FILE_SOURCE_CONNECTORS:
        return "duckdb"
    return ct


# ── Plan: intake -> draft -> build ──────────────────────────────────────────


async def start_dashboard_intake(
    db: AsyncSession,
    org_id: UUID,
    goal: str,
) -> dict[str, Any]:
    """Return connections + clarifying questions. No DB writes."""
    conns = await ConnectionRepository(db).list_by_org(org_id)
    connections = [
        {
            "id": str(c.id),
            "name": c.name,
            "connector_type": c.connector_type or "postgresql",
            "description": c.database_name,
        }
        for c in conns
        if c.deleted_at is None
    ]
    default_connection_id = connections[0]["id"] if connections else None

    if not connections:
        return {
            "questions": [],
            "connections": [],
            "message": (
                "I couldn't find a data connection for your org yet. Add one in "
                "Settings → Connections, then ask me to build the dashboard again."
            ),
        }

    return {
        "dashboard_intake": {"active": True, "goal": goal[:500]},
        "connections": connections,
        "default_connection_id": default_connection_id,
        "questions": _build_intake_questions(connections),
        "message": (
            "A few quick questions so I build the right thing. "
            + (
                "Pick which connection to use and "
                if len(connections) > 1
                else ""
            )
            + "tell me what to track; I'll draft a plan for you to review before anything is saved."
        ),
    }


async def draft_dashboard_plan(
    db: AsyncSession,
    org_id: UUID,
    *,
    connection_id: UUID | None = None,
    goal: str,
    time_window: str | None = None,
    segments: str | None = None,
    filters_to_parameterize: str | None = None,
    compare_period: str | None = None,
    max_charts: int = 4,
    dashboard_name: str | None = None,
) -> dict[str, Any]:
    """LLM-generated plan only — nothing persisted. Returns a DraftPlanArtifact."""
    from app.services.studio_query_service import _is_select_only

    # Cap charts to keep the draft within the chat request timeout (each chart adds
    # ~6s of LLM generation); users can add more later via the edit flow.
    max_charts = max(1, min(max_charts, 4))
    # Default to the org's first active connection (e.g. single-connection orgs
    # where intake never asked the user to choose one).
    if connection_id is None:
        active = [c for c in await ConnectionRepository(db).list_by_org(org_id) if c.deleted_at is None]
        if not active:
            return {"error": "No data connection found for this org"}
        connection_id = active[0].id

    schema_context = await _introspect_schema(db, org_id, connection_id)
    if not schema_context:
        return {"error": "No tables found in the selected connection"}

    conn_row = await ConnectionRepository(db).get_by_id(connection_id)
    dialect = _normalize_dialect(conn_row.connector_type if conn_row else None)
    connection_name = conn_row.name if conn_row else None

    context_lines = [f"Goal: {goal}", f"Database dialect: {dialect}"]
    if time_window:
        context_lines.append(f"Default time window: {time_window}")
    if segments:
        context_lines.append(f"Segmentation: {segments}")
    if filters_to_parameterize:
        context_lines.append(f"User-filterable dimensions: {filters_to_parameterize}")
    if compare_period:
        context_lines.append(f"Comparison period: {compare_period}")

    system_prompt = (
        "You are a senior data analyst designing an Entivia Studio dashboard. "
        "Given schema and requirements, return a JSON object with:\n"
        '  "dashboard_name": string,\n'
        '  "description": string,\n'
        '  "charts": array of objects, each with:\n'
        '    "query_name", "description", "sql" (single SELECT, dialect-correct),\n'
        '    "params": [{name, type, default_value, label}] for each {{placeholder}} in sql,\n'
        '    "chart_type" (bar|line|area|pie|scatter|table|number),\n'
        '    "config" (x_axis, y_axis, title, etc.).\n'
        "Rules:\n"
        "- Use {{param_name}} for filterable values (dates, regions, statuses).\n"
        "- For time filtering use {{__time_from}} and {{__time_to}}; declare them with "
        'type "datetime" and default_value null (the dashboard supplies them at runtime).\n'
        "- Param default_value must be a literal (e.g. a YYYY-MM-DD date, a number, a string), "
        "never a SQL expression like CURRENT_DATE or date_trunc(...).\n"
        "- For categorical filters (account_type, status, region, etc.) declare type \"string\", "
        "default_value \"ALL\", and guard the SQL so ALL means no filter: "
        "({{account_type}} = 'ALL' OR account_type = {{account_type}}). Every non-time param needs "
        "a literal default so charts render without user input.\n"
        "- You do NOT know the exact casing of stored text values. For any equality on a text "
        "column (status, type, category, region, etc.) match case-insensitively with LOWER(): e.g. "
        "WHERE LOWER(status) = 'active', and for filters "
        "({{status}} = 'ALL' OR LOWER(status) = LOWER({{status}})).\n"
        "- The schema lists each column's data type in parentheses. A date/time column stored as "
        "text/varchar/char MUST be cast before date functions or comparisons. Use CAST(...) syntax, "
        "NEVER the :: operator (it breaks parameter binding). Example: "
        "DATE_TRUNC('month', CAST(open_date AS TIMESTAMP)) and "
        "CAST(open_date AS TIMESTAMP) >= CAST({{__time_from}} AS TIMESTAMP). "
        "Only cast columns whose type is textual.\n"
        "- Only SELECT statements. No markdown.\n"
        f"- At most {max_charts} charts.\n"
        "Output ONLY valid JSON."
    )
    user_prompt = f"Schema:\n{schema_context}\n\nRequirements:\n" + "\n".join(context_lines)

    from app.infrastructure.llm.factory import get_llm_client

    llm = get_llm_client()
    if not llm.is_configured():
        return {"error": f"AI provider '{llm.provider_name}' is not configured"}

    try:
        raw_text = await llm.complete(system_prompt, user_prompt, max_tokens=8000, temperature=0.1)
    except Exception as exc:
        logger.warning("[draft_dashboard_plan] LLM call failed: %s", exc)
        return {"error": "Could not generate a dashboard plan — please try again"}
    try:
        plan_obj = _extract_json_object(raw_text)
    except Exception as exc:
        plan_obj = _salvage_plan(raw_text)
        if not plan_obj:
            logger.warning("[draft_dashboard_plan] JSON parse + salvage failed: %s", exc)
            return {"error": "Could not generate a dashboard plan — please try again"}
        logger.info("[draft_dashboard_plan] salvaged %d chart(s) from truncated JSON", len(plan_obj.get("charts") or []))

    safe_charts: list[dict[str, Any]] = []
    for spec in plan_obj.get("charts") or []:
        if not isinstance(spec, dict) or not spec.get("sql"):
            continue
        if not _is_select_only(str(spec["sql"])):
            continue
        title = str(spec.get("query_name") or spec.get("name") or "Chart")
        safe_charts.append({
            "name": title,
            "query_name": title,
            "description": str(spec.get("description") or ""),
            "chart_type": str(spec.get("chart_type") or "table"),
            "sql": _clean_chart_sql(spec["sql"]),
            "config": spec.get("config") or {},
            "params": _normalize_params(spec.get("params") or []),
        })
    if not safe_charts:
        return {"error": "Generated SQL was not safe — only SELECT statements are allowed"}

    final_name = (dashboard_name or plan_obj.get("dashboard_name") or goal[:80] or "Dashboard").strip()
    plan = {
        "name": final_name[:120] or "Dashboard",
        "description": str(plan_obj.get("description") or goal[:200]),
        "connection_id": str(connection_id),
        "connection_name": connection_name,
        "is_public": False,
        "time_range": _time_range_from_window(time_window),
        "dashboard_params": _aggregate_dashboard_params(safe_charts),
        "charts": safe_charts,
        "chart_count": len(safe_charts),
    }
    return {
        "plan": plan,
        "message": (
            f"Here's a plan for '{plan['name']}' with {len(safe_charts)} chart(s). "
            "Review it, then confirm to build — or tell me what to change. "
            "Nothing is saved yet."
        ),
    }


async def build_dashboard_from_plan(
    db: AsyncSession,
    org_id: UUID,
    current_user: Any,
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Persist a previously approved plan. Returns a BuildDashboardArtifact."""
    from app.api.dependencies.plan_gate import check_studio_dashboard_limit
    from app.api.errors import PulseHTTPException
    from app.infrastructure.audit import log_audit
    from app.infrastructure.database.repositories.studio_dashboard_item_repository import (
        StudioDashboardItemRepository,
    )
    from app.infrastructure.database.repositories.studio_dashboard_repository import (
        StudioDashboardRepository,
    )
    from app.infrastructure.database.repositories.studio_query_repository import (
        StudioQueryRepository,
    )
    from app.infrastructure.database.repositories.studio_visualization_repository import (
        StudioVisualizationRepository,
    )
    from app.services.studio_query_service import _inject_limit, _is_select_only

    try:
        await check_studio_dashboard_limit(db, org_id)
    except PulseHTTPException as exc:
        detail = exc.detail
        msg = detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail)
        return {"error": msg}
    except Exception as exc:
        return {"error": str(exc)}

    charts = plan.get("charts") or []
    if not charts:
        return {"error": "Plan has no charts"}

    connection_id_raw = plan.get("connection_id")
    if not connection_id_raw:
        return {"error": "Plan missing connection_id"}
    connection_id = UUID(str(connection_id_raw))

    query_repo = StudioQueryRepository(db)
    viz_repo = StudioVisualizationRepository(db)
    dash_repo = StudioDashboardRepository(db)
    item_repo = StudioDashboardItemRepository(db)

    created_vizs = []
    for spec in charts:
        sql = _clean_chart_sql(spec.get("sql", ""))
        if not _is_select_only(sql):
            continue
        safe_sql = _inject_limit(sql, 5000)
        title = str(spec.get("query_name") or spec.get("name") or "Query")
        q = await query_repo.create(
            org_id,
            current_user.id,
            name=title,
            description=str(spec.get("description", "")),
            sql_text=safe_sql,
            connection_id=connection_id,
            params=spec.get("params") or [],
        )
        viz = await viz_repo.create(
            org_id,
            q.id,
            current_user.id,
            name=title,
            chart_type=str(spec.get("chart_type", "table")),
            config={k: v for k, v in (spec.get("config") or {}).items() if v is not None},
        )
        created_vizs.append(viz)

    if not created_vizs:
        return {"error": "No valid charts in plan"}

    dashboard_name = str(plan.get("name") or plan.get("dashboard_name") or "Dashboard")[:255]
    is_public = bool(plan.get("is_public", False))
    slug: str | None = None
    if is_public:
        base = re.sub(r"[^a-z0-9]+", "-", dashboard_name.lower()).strip("-")[:60]
        for _ in range(5):
            candidate = f"{base}-{secrets.token_hex(2)}"
            if not await dash_repo.slug_exists(candidate):
                slug = candidate
                break

    dashboard = await dash_repo.create(
        org_id,
        current_user.id,
        name=dashboard_name,
        description=str(plan.get("description") or "")[:2000],
        is_public=is_public,
        slug=slug,
        layout=[],
        dashboard_params=plan.get("dashboard_params") or [],
        time_range=plan.get("time_range"),
    )

    layout = []
    for i, viz in enumerate(created_vizs):
        item = await item_repo.create(org_id, dashboard.id, viz.id, i)
        layout.append({"item_id": str(item.id), "x": (i % 2) * 6, "y": (i // 2) * 4, "w": 6, "h": 4})
    await dash_repo.update(dashboard, layout=layout)
    await db.commit()

    frontend = (settings.FRONTEND_URL or "").rstrip("/")
    url = f"{frontend}/studio/dashboards/{dashboard.id}"
    if slug and is_public:
        url = f"{frontend}/studio/dashboards/public/{slug}"

    try:
        await log_audit(
            db,
            org_id=org_id,
            user_id=current_user.id,
            action="studio.agent_build_dashboard",
            metadata={"dashboard_name": dashboard_name, "charts": len(created_vizs)},
        )
    except Exception:
        pass

    return {
        "dashboard": {
            "id": str(dashboard.id),
            "name": dashboard_name,
            "url": url,
            "chart_count": len(created_vizs),
        },
        "is_public": is_public,
        "slug": slug,
        "message": f"Dashboard '{dashboard_name}' is live with {len(created_vizs)} chart(s). Open: {url}",
    }


# ── Iterate: propose -> apply changes ───────────────────────────────────────

_ALWAYS_ALLOWED_CHANGE_ACTIONS = frozenset({
    "rename", "set_description", "remove_chart",
    "set_dashboard_params", "set_time_range", "set_public",
})
_NEW_SQL_CHANGE_ACTIONS = frozenset({"add_chart", "replace_chart"})


def _allowed_change_actions() -> frozenset[str]:
    if settings.DASHBOARD_ITERATION_ALLOW_NEW_SQL:
        return _ALWAYS_ALLOWED_CHANGE_ACTIONS | _NEW_SQL_CHANGE_ACTIONS
    return _ALWAYS_ALLOWED_CHANGE_ACTIONS


async def _load_dashboard_snapshot(
    db: AsyncSession,
    org_id: UUID,
    dashboard_id: UUID,
) -> dict[str, Any] | None:
    """Read the current dashboard, its items, and their visualizations + queries."""
    from app.infrastructure.database.repositories.studio_dashboard_item_repository import (
        StudioDashboardItemRepository,
    )
    from app.infrastructure.database.repositories.studio_dashboard_repository import (
        StudioDashboardRepository,
    )
    from app.infrastructure.database.repositories.studio_query_repository import (
        StudioQueryRepository,
    )
    from app.infrastructure.database.repositories.studio_visualization_repository import (
        StudioVisualizationRepository,
    )

    dash_repo = StudioDashboardRepository(db)
    item_repo = StudioDashboardItemRepository(db)
    viz_repo = StudioVisualizationRepository(db)
    query_repo = StudioQueryRepository(db)

    dashboard = await dash_repo.get_by_id_and_org(dashboard_id, org_id)
    if dashboard is None:
        return None

    items = await item_repo.list_by_dashboard(dashboard_id, org_id)
    items_payload: list[dict[str, Any]] = []
    for item in items:
        viz = None
        q = None
        if item.visualization_id is not None:
            viz = await viz_repo.get_by_id_and_org(item.visualization_id, org_id)
            if viz is not None:
                q = await query_repo.get_by_id_and_org(viz.query_id, org_id)
        items_payload.append({
            "item_id": str(item.id),
            "position": item.position,
            "panel_type": item.panel_type,
            "visualization": (
                {
                    "id": str(viz.id),
                    "name": viz.name,
                    "chart_type": viz.chart_type,
                    "config": viz.config or {},
                }
                if viz is not None
                else None
            ),
            "query": (
                {
                    "id": str(q.id),
                    "name": q.name,
                    "sql_text": q.sql_text,
                    "params": q.params or [],
                    "connection_id": str(q.connection_id) if q.connection_id else None,
                }
                if q is not None
                else None
            ),
        })

    return {
        "dashboard": {
            "id": str(dashboard.id),
            "name": dashboard.name,
            "description": dashboard.description,
            "is_public": dashboard.is_public,
            "slug": dashboard.slug,
            "dashboard_params": dashboard.dashboard_params or [],
            "time_range": dashboard.time_range or {},
            "layout": dashboard.layout or [],
        },
        "items": items_payload,
    }


def _first_connection_id_from_snapshot(snapshot: dict[str, Any]) -> str | None:
    for it in snapshot.get("items") or []:
        q = it.get("query") or {}
        cid = q.get("connection_id")
        if cid:
            return cid
    return None


def _decorate_change_for_ui(change: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Add display-only fields the frontend cards read (old_name, chart_name, spec.name)."""
    action = change.get("action")
    if action == "rename":
        change.setdefault("old_name", snapshot.get("dashboard", {}).get("name"))
    elif action in ("remove_chart", "replace_chart"):
        item_id = change.get("item_id")
        for it in snapshot.get("items") or []:
            if it.get("item_id") == item_id and it.get("visualization"):
                name = it["visualization"].get("name")
                if action == "remove_chart":
                    change.setdefault("chart_name", name)
                else:
                    change.setdefault("old_chart_name", name)
                break
    if action in ("add_chart", "replace_chart"):
        spec = change.get("spec") or {}
        if "name" not in spec and spec.get("query_name"):
            spec["name"] = spec["query_name"]
            change["spec"] = spec
    return change


async def propose_dashboard_changes(
    db: AsyncSession,
    org_id: UUID,
    *,
    dashboard_id: UUID,
    feedback: str,
) -> dict[str, Any]:
    """Read-only: turn natural-language feedback into a list of structured changes."""
    from app.services.studio_query_service import _is_select_only

    snapshot = await _load_dashboard_snapshot(db, org_id, dashboard_id)
    if snapshot is None:
        return {"error": "Dashboard not found"}

    connection_id_str = _first_connection_id_from_snapshot(snapshot)
    dialect = "postgresql"
    schema_context = ""
    if connection_id_str:
        try:
            conn_row = await ConnectionRepository(db).get_by_id(UUID(connection_id_str))
            if conn_row is not None:
                dialect = _normalize_dialect(conn_row.connector_type)
            schema_context = await _introspect_schema(db, org_id, UUID(connection_id_str))
        except Exception as exc:
            logger.warning("[propose_dashboard_changes] schema fetch failed: %s", exc)

    system_prompt = (
        "You are a senior data analyst iterating on an Entivia Studio dashboard. "
        "Given the current dashboard JSON and user feedback, return a JSON object with:\n"
        '  "summary": short plain-English description of the proposed changes,\n'
        '  "changes": array. Each change object is one of:\n'
        '    {"action": "rename", "new_name": string},\n'
        '    {"action": "set_description", "new_description": string},\n'
        '    {"action": "remove_chart", "item_id": string},\n'
        '    {"action": "add_chart", "spec": {query_name, description, sql, chart_type, config, params}},\n'
        '    {"action": "replace_chart", "item_id": string, "spec": {query_name, sql, chart_type, config, params}},\n'
        '    {"action": "set_dashboard_params", "params": [...]},\n'
        '    {"action": "set_time_range", "time_range": {...}},\n'
        '    {"action": "set_public", "is_public": bool}.\n'
        "Rules:\n"
        "- Only SELECT statements; use {{param}} for filterable values.\n"
        f"- SQL must be valid {dialect}.\n"
        "- Reference item_ids exactly as they appear in the snapshot.\n"
        "- If feedback is unclear, return changes=[] and explain in summary.\n"
        "Output ONLY valid JSON. No markdown."
    )
    user_prompt = (
        f"Schema:\n{schema_context}\n\n"
        f"Current dashboard:\n{json.dumps(snapshot, default=str)[:8000]}\n\n"
        f"User feedback:\n{feedback}"
    )

    from app.infrastructure.llm.factory import get_llm_client

    llm = get_llm_client()
    if not llm.is_configured():
        return {"error": f"AI provider '{llm.provider_name}' is not configured"}

    try:
        raw_text = await llm.complete(system_prompt, user_prompt, max_tokens=8000, temperature=0.1)
        proposal = _extract_json_object(raw_text)
    except Exception as exc:
        logger.warning("[propose_dashboard_changes] LLM failed: %s", exc)
        return {"error": "Could not interpret the feedback — please rephrase"}

    valid_item_ids = {it["item_id"] for it in snapshot["items"]}
    safe_changes: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    allowed = _allowed_change_actions()

    for change in proposal.get("changes") or []:
        if not isinstance(change, dict):
            continue
        action = change.get("action")
        if action not in allowed:
            reason = "new_sql_disabled" if action in _NEW_SQL_CHANGE_ACTIONS else "unknown_action"
            rejected.append({"change": change, "reason": reason})
            continue
        if action in ("remove_chart", "replace_chart") and change.get("item_id") not in valid_item_ids:
            rejected.append({"change": change, "reason": "unknown_item_id"})
            continue
        if action in ("add_chart", "replace_chart"):
            sql = str((change.get("spec") or {}).get("sql", "")).strip()
            if not sql or not _is_select_only(sql):
                rejected.append({"change": change, "reason": "unsafe_or_missing_sql"})
                continue
        safe_changes.append(_decorate_change_for_ui(change, snapshot))

    return {
        "dashboard_id": str(dashboard_id),
        "dashboard_name": snapshot["dashboard"].get("name"),
        "summary": str(proposal.get("summary") or "Proposed changes ready for review."),
        "changes": safe_changes,
        "rejected": rejected,
        "message": (
            "Review the proposed changes. Confirm and I'll apply them, "
            "or describe further adjustments."
        ),
    }


async def apply_dashboard_changes(
    db: AsyncSession,
    org_id: UUID,
    current_user: Any,
    *,
    dashboard_id: UUID,
    changes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Persist a list of approved dashboard changes (one transaction)."""
    from app.infrastructure.audit import log_audit
    from app.infrastructure.database.repositories.studio_dashboard_item_repository import (
        StudioDashboardItemRepository,
    )
    from app.infrastructure.database.repositories.studio_dashboard_repository import (
        StudioDashboardRepository,
    )
    from app.infrastructure.database.repositories.studio_query_repository import (
        StudioQueryRepository,
    )
    from app.infrastructure.database.repositories.studio_visualization_repository import (
        StudioVisualizationRepository,
    )
    from app.services.studio_query_service import _inject_limit, _is_select_only

    if not changes:
        return {"error": "No changes provided"}

    dash_repo = StudioDashboardRepository(db)
    item_repo = StudioDashboardItemRepository(db)
    viz_repo = StudioVisualizationRepository(db)
    query_repo = StudioQueryRepository(db)

    dashboard = await dash_repo.get_by_id_and_org(dashboard_id, org_id)
    if dashboard is None:
        return {"error": "Dashboard not found"}

    items = await item_repo.list_by_dashboard(dashboard_id, org_id)
    item_by_id = {str(it.id): it for it in items}

    snapshot = await _load_dashboard_snapshot(db, org_id, dashboard_id)
    connection_id_str = _first_connection_id_from_snapshot(snapshot or {})

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    update_fields: dict[str, Any] = {}
    next_position = max((it.position for it in items), default=-1) + 1
    allowed = _allowed_change_actions()

    for change in changes:
        if not isinstance(change, dict):
            skipped.append({"change": change, "reason": "not_an_object"})
            continue
        action = change.get("action")
        if action not in allowed:
            reason = "new_sql_disabled" if action in _NEW_SQL_CHANGE_ACTIONS else "unknown_action"
            skipped.append({"change": change, "reason": reason})
            continue
        try:
            if action == "rename":
                name = str(change.get("new_name") or "").strip()[:255]
                if not name:
                    skipped.append({"change": change, "reason": "empty_name"})
                    continue
                update_fields["name"] = name
            elif action == "set_description":
                update_fields["description"] = str(change.get("new_description") or "").strip()[:2000]
            elif action == "set_public":
                update_fields["is_public"] = bool(change.get("is_public"))
            elif action == "set_dashboard_params":
                params = change.get("params")
                if not isinstance(params, list):
                    skipped.append({"change": change, "reason": "params_must_be_list"})
                    continue
                update_fields["dashboard_params"] = params
            elif action == "set_time_range":
                tr = change.get("time_range")
                if tr is not None and not isinstance(tr, dict):
                    skipped.append({"change": change, "reason": "time_range_must_be_object"})
                    continue
                update_fields["time_range"] = tr or {}
            elif action == "remove_chart":
                item = item_by_id.get(str(change.get("item_id") or ""))
                if item is None:
                    skipped.append({"change": change, "reason": "unknown_item_id"})
                    continue
                await item_repo.delete(item)
                item_by_id.pop(str(item.id), None)
            elif action == "add_chart":
                if connection_id_str is None:
                    skipped.append({"change": change, "reason": "no_connection_on_dashboard"})
                    continue
                spec = change.get("spec") or {}
                sql = _clean_chart_sql(spec.get("sql", ""))
                if not _is_select_only(sql):
                    skipped.append({"change": change, "reason": "unsafe_sql"})
                    continue
                title = str(spec.get("query_name") or spec.get("name") or "Query")
                q = await query_repo.create(
                    org_id, current_user.id,
                    name=title,
                    description=str(spec.get("description", "")),
                    sql_text=_inject_limit(sql, 5000),
                    connection_id=UUID(connection_id_str),
                    params=_normalize_params(spec.get("params") or []),
                )
                viz = await viz_repo.create(
                    org_id, q.id, current_user.id,
                    name=title,
                    chart_type=str(spec.get("chart_type", "table")),
                    config={k: v for k, v in (spec.get("config") or {}).items() if v is not None},
                )
                new_item = await item_repo.create(org_id, dashboard_id, viz.id, next_position)
                next_position += 1
                item_by_id[str(new_item.id)] = new_item
            elif action == "replace_chart":
                item = item_by_id.get(str(change.get("item_id") or ""))
                if item is None or item.visualization_id is None:
                    skipped.append({"change": change, "reason": "unknown_item_id"})
                    continue
                spec = change.get("spec") or {}
                sql = _clean_chart_sql(spec.get("sql", ""))
                if not _is_select_only(sql):
                    skipped.append({"change": change, "reason": "unsafe_sql"})
                    continue
                viz = await viz_repo.get_by_id_and_org(item.visualization_id, org_id)
                if viz is None:
                    skipped.append({"change": change, "reason": "viz_missing"})
                    continue
                q = await query_repo.get_by_id_and_org(viz.query_id, org_id)
                if q is None:
                    skipped.append({"change": change, "reason": "query_missing"})
                    continue
                q.sql_text = _inject_limit(sql, 5000)
                if spec.get("query_name"):
                    q.name = str(spec["query_name"])[:255]
                if isinstance(spec.get("params"), list):
                    q.params = _normalize_params(spec["params"])
                await viz_repo.update(
                    viz,
                    chart_type=str(spec.get("chart_type") or viz.chart_type),
                    config={k: v for k, v in (spec.get("config") or {}).items() if v is not None}
                    or viz.config,
                )
            applied.append(change)
        except Exception as exc:
            logger.warning("[apply_dashboard_changes] %s failed: %s", action, exc)
            skipped.append({"change": change, "reason": f"error: {exc}"})

    if update_fields:
        await dash_repo.update(dashboard, **update_fields)
    await db.commit()

    try:
        await log_audit(
            db,
            org_id=org_id,
            user_id=current_user.id,
            action="studio.agent_iterate_dashboard",
            metadata={"dashboard_id": str(dashboard_id), "applied": len(applied), "skipped": len(skipped)},
        )
    except Exception:
        pass

    frontend = (settings.FRONTEND_URL or "").rstrip("/")
    url = f"{frontend}/studio/dashboards/{dashboard_id}"
    return {
        "dashboard_id": str(dashboard_id),
        "dashboard_name": dashboard.name,
        "applied": applied,
        "skipped": skipped,
        "url": url,
        "message": (
            f"Applied {len(applied)} change(s) to '{dashboard.name}'."
            + (f" Skipped {len(skipped)}." if skipped else "")
            + f" Open: {url}"
        ),
    }
