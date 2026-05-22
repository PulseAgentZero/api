"""Analyst-style dashboard builder: intake → plan → confirm → build."""

from __future__ import annotations

import json
import logging
import re
import secrets
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.infrastructure.database.repositories.connection_repository import ConnectionRepository
from app.services.studio_ai_service import _introspect_schema

logger = logging.getLogger(__name__)

_INTAKE_QUESTION_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "dashboard_name",
        "question": "What would you like to name this dashboard?",
        "required": True,
        "type": "text",
        "max_length": 120,
    },
    {
        "id": "business_goal",
        "question": "What business question should this dashboard answer? (Be specific: metric, audience, decision it supports.)",
        "required": True,
    },
    {
        "id": "success_metric",
        "question": "What is the primary success metric or KPI you want to track?",
        "required": True,
    },
    {
        "id": "time_window",
        "question": "What time window should charts default to? (e.g. last 30 days, last quarter, year-to-date)",
        "required": True,
    },
    {
        "id": "segments",
        "question": "How should data be broken down or segmented? (e.g. by region, product, tier, channel)",
        "required": False,
    },
    {
        "id": "filters_to_parameterize",
        "question": "Which filters should users control on the dashboard? (e.g. date range, region, status — these become dropdowns)",
        "required": False,
    },
    {
        "id": "compare_period",
        "question": "Do you want a comparison period (e.g. vs prior month or prior year)?",
        "required": False,
    },
    {
        "id": "refresh_cadence",
        "question": "How often should data refresh? (manual, hourly, daily)",
        "required": False,
    },
]


def _extract_json_array(raw_text: str) -> list[dict]:
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("Expected JSON array")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, list):
        raise ValueError("Expected JSON array")
    return [x for x in data if isinstance(x, dict)]


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


def _build_intake_questions(
    connections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    questions = list(_INTAKE_QUESTION_TEMPLATES)
    if len(connections) > 1:
        questions.insert(
            1,
            {
                "id": "connection_id",
                "question": "Which data connection should this dashboard use?",
                "required": True,
                "type": "choice",
                "options": [
                    {
                        "value": c["id"],
                        "label": f"{c['name']} ({c['dialect']}, {c['status']})",
                    }
                    for c in connections
                ],
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
    seen: dict[str, dict] = {}
    for chart in charts:
        for p in chart.get("params") or []:
            if not isinstance(p, dict):
                continue
            name = p.get("name")
            if name and name not in seen:
                seen[name] = {
                    "name": name,
                    "type": p.get("type", "text"),
                    "default_value": p.get("default_value"),
                    "label": p.get("label", name.replace("_", " ").title()),
                }
    params = list(seen.values())
    if params and not any(p["name"] in ("__time_from", "__time_to") for p in params):
        params.extend([
            {"name": "__time_from", "type": "date", "label": "From", "default_value": None},
            {"name": "__time_to", "type": "date", "label": "To", "default_value": None},
        ])
    return params


async def start_dashboard_intake(
    db: AsyncSession,
    org_id: UUID,
    goal: str,
) -> dict[str, Any]:
    """Return connections, schema preview, and clarifying questions (no DB writes)."""
    conns = await ConnectionRepository(db).list_by_org(org_id)
    connections = [
        {
            "id": str(c.id),
            "name": c.name,
            "dialect": c.connector_type or "postgresql",
            "status": c.status,
        }
        for c in conns
        if c.deleted_at is None
    ]

    default_connection_id: str | None = connections[0]["id"] if connections else None
    schema_preview = ""
    if default_connection_id:
        try:
            schema_preview = await _introspect_schema(
                db, org_id, UUID(default_connection_id),
            )
        except Exception as exc:
            logger.warning("[dashboard_intake] schema preview failed: %s", exc)

    return {
        "dashboard_intake": {"active": True, "goal": goal[:500]},
        "connections": connections,
        "default_connection_id": default_connection_id,
        "schema_preview": schema_preview[:4000] if schema_preview else "",
        "questions": _build_intake_questions(connections),
        "message": (
            "Before building, please answer the clarifying questions below. "
            "If multiple connections exist, specify which one to use."
        ),
    }


async def draft_dashboard_plan(
    db: AsyncSession,
    org_id: UUID,
    *,
    connection_id: UUID,
    goal: str,
    time_window: str | None = None,
    segments: str | None = None,
    filters_to_parameterize: str | None = None,
    compare_period: str | None = None,
    max_charts: int = 4,
    dashboard_name: str | None = None,
) -> dict[str, Any]:
    """LLM-generated plan only — nothing persisted."""
    from app.services.studio_query_service import _is_select_only

    max_charts = max(1, min(max_charts, 6))
    schema_context = await _introspect_schema(db, org_id, connection_id)
    if not schema_context:
        return {"error": "No tables found in the selected connection"}

    conn_row = await ConnectionRepository(db).get_by_id(connection_id)
    dialect = _normalize_dialect(conn_row.connector_type if conn_row else None)

    context_lines = [
        f"Goal: {goal}",
        f"Database dialect: {dialect}",
    ]
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
        "- Include __time_from and __time_to in params when time filtering applies.\n"
        "- Only SELECT statements. No markdown.\n"
        f"- At most {max_charts} charts.\n"
        "Output ONLY valid JSON."
    )
    user_prompt = (
        f"Schema:\n{schema_context}\n\n"
        f"Requirements:\n" + "\n".join(context_lines)
    )

    from app.infrastructure.llm.factory import get_llm_client

    llm = get_llm_client()
    if not llm.is_configured():
        return {"error": f"AI provider '{llm.provider_name}' is not configured"}

    try:
        raw_text = await llm.complete(system_prompt, user_prompt, max_tokens=2500, temperature=0.1)
        plan_obj = _extract_json_object(raw_text)
    except Exception as exc:
        logger.warning("[draft_dashboard_plan] LLM failed: %s", exc)
        return {"error": "Could not generate a dashboard plan — please try again"}

    charts = plan_obj.get("charts") or []
    safe_charts = []
    for spec in charts:
        if not isinstance(spec, dict) or not spec.get("sql"):
            continue
        if not _is_select_only(str(spec["sql"])):
            continue
        safe_charts.append(spec)
    if not safe_charts:
        return {"error": "Generated SQL was not safe — only SELECT statements are allowed"}

    dashboard_params = _aggregate_dashboard_params(safe_charts)
    time_range = _time_range_from_window(time_window)

    final_name = (dashboard_name or plan_obj.get("dashboard_name") or goal[:80]).strip()
    plan = {
        "connection_id": str(connection_id),
        "dashboard_name": final_name[:120] or "Dashboard",
        "description": plan_obj.get("description") or goal[:200],
        "is_public": False,
        "time_range": time_range,
        "dashboard_params": dashboard_params,
        "charts": safe_charts,
        "chart_count": len(safe_charts),
    }
    return {
        "plan": plan,
        "message": (
            f"Review the proposed plan for '{plan['dashboard_name']}'. "
            "When it looks right, confirm and I will build it. "
            "Ask for changes if you want a different name, charts, or filters."
        ),
    }


async def build_dashboard_from_plan(
    db: AsyncSession,
    org_id: UUID,
    current_user: Any,
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Persist a previously approved plan."""
    from app.api.dependencies.plan_gate import check_studio_dashboard_limit
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
    except Exception as exc:
        from app.api.errors import PulseHTTPException
        if isinstance(exc, PulseHTTPException):
            detail = exc.detail
            msg = detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail)
            return {"error": msg}
        return {"error": str(exc)}

    charts = plan.get("charts") or []
    if not charts:
        return {"error": "Plan has no charts"}

    connection_id_raw = plan.get("connection_id")
    if not connection_id_raw:
        return {"error": "Plan missing connection_id"}
    connection_id = UUID(str(connection_id_raw))

    conn_row = await ConnectionRepository(db).get_by_id(connection_id)
    dialect = _normalize_dialect(conn_row.connector_type if conn_row else None)

    query_repo = StudioQueryRepository(db)
    viz_repo = StudioVisualizationRepository(db)
    dash_repo = StudioDashboardRepository(db)
    item_repo = StudioDashboardItemRepository(db)

    created_vizs = []
    for spec in charts:
        sql = str(spec.get("sql", "")).strip()
        if not _is_select_only(sql):
            continue
        safe_sql = _inject_limit(sql, 5000, dialect=dialect)
        params = spec.get("params") or []
        q = await query_repo.create(
            org_id,
            current_user.id,
            name=str(spec.get("query_name", "Query")),
            description=str(spec.get("description", "")),
            sql_text=safe_sql,
            connection_id=connection_id,
            params=params,
        )
        viz = await viz_repo.create(
            org_id,
            q.id,
            current_user.id,
            name=str(spec.get("query_name", "Chart")),
            chart_type=str(spec.get("chart_type", "table")),
            config={k: v for k, v in (spec.get("config") or {}).items() if v is not None},
        )
        created_vizs.append(viz)

    if not created_vizs:
        return {"error": "No valid charts in plan"}

    dashboard_name = str(plan.get("dashboard_name") or "Dashboard")[:255]
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
        "dashboard_id": str(dashboard.id),
        "dashboard_name": dashboard_name,
        "chart_count": len(created_vizs),
        "is_public": is_public,
        "slug": slug,
        "url": url,
        "message": f"Dashboard '{dashboard_name}' created with {len(created_vizs)} chart(s). Open: {url}",
    }


_FILE_SOURCE_CONNECTORS = frozenset({"csv", "excel", "google_sheets", "s3", "gcs"})


def _normalize_dialect(connector_type: str | None) -> str:
    """Map connector_type to the SQL dialect the LLM should target.

    File-source connectors run on DuckDB inside Studio. Warehouse connectors keep
    their own name (bigquery, snowflake, redshift, clickhouse). Empty defaults to
    postgresql.
    """
    ct = (connector_type or "").lower().strip()
    if not ct:
        return "postgresql"
    if ct in _FILE_SOURCE_CONNECTORS:
        return "duckdb"
    return ct


_ALWAYS_ALLOWED_CHANGE_ACTIONS = frozenset({
    "rename",
    "set_description",
    "remove_chart",
    "set_dashboard_params",
    "set_time_range",
    "set_public",
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


async def propose_dashboard_changes(
    db: AsyncSession,
    org_id: UUID,
    *,
    dashboard_id: UUID,
    feedback: str,
) -> dict[str, Any]:
    """Read-only: ask the LLM for a list of structured changes from natural language feedback."""
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
            schema_context = await _introspect_schema(
                db, org_id, UUID(connection_id_str),
            )
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
        '    {"action": "add_chart", "spec": {query_name, description, sql, chart_type, '
        'config, params}},\n'
        '    {"action": "replace_chart", "item_id": string, "spec": {query_name, sql, '
        'chart_type, config, params}},\n'
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
        raw_text = await llm.complete(system_prompt, user_prompt, max_tokens=2500, temperature=0.1)
        proposal = _extract_json_object(raw_text)
    except Exception as exc:
        logger.warning("[propose_dashboard_changes] LLM failed: %s", exc)
        return {"error": "Could not interpret the feedback — please rephrase"}

    raw_changes = proposal.get("changes") or []
    valid_item_ids = {it["item_id"] for it in snapshot["items"]}
    safe_changes: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    allowed = _allowed_change_actions()
    for change in raw_changes:
        if not isinstance(change, dict):
            continue
        action = change.get("action")
        if action not in allowed:
            reason = (
                "new_sql_disabled"
                if action in _NEW_SQL_CHANGE_ACTIONS
                else "unknown_action"
            )
            rejected.append({"change": change, "reason": reason})
            continue
        if action in ("remove_chart", "replace_chart"):
            item_id = change.get("item_id")
            if item_id not in valid_item_ids:
                rejected.append({"change": change, "reason": "unknown_item_id"})
                continue
        if action in ("add_chart", "replace_chart"):
            spec = change.get("spec") or {}
            sql = str(spec.get("sql", "")).strip()
            if not sql or not _is_select_only(sql):
                rejected.append({"change": change, "reason": "unsafe_or_missing_sql"})
                continue
        safe_changes.append(change)

    return {
        "dashboard_id": str(dashboard_id),
        "summary": str(proposal.get("summary") or "Proposed changes ready for review."),
        "changes": safe_changes,
        "rejected": rejected,
        "current": snapshot,
        "message": (
            "Review the proposed changes. Confirm and I will apply them, "
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
    dialect = "postgresql"
    if connection_id_str:
        try:
            conn_row = await ConnectionRepository(db).get_by_id(UUID(connection_id_str))
            if conn_row is not None:
                dialect = _normalize_dialect(conn_row.connector_type)
        except Exception:
            pass

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    new_dashboard_params: list[dict[str, Any]] | None = None
    new_time_range: dict[str, Any] | None = None
    new_name: str | None = None
    new_description: str | None = None
    new_public: bool | None = None

    next_position = max((it.position for it in items), default=-1) + 1

    allowed = _allowed_change_actions()
    for change in changes:
        if not isinstance(change, dict):
            skipped.append({"change": change, "reason": "not_an_object"})
            continue
        action = change.get("action")
        if action not in allowed:
            reason = (
                "new_sql_disabled"
                if action in _NEW_SQL_CHANGE_ACTIONS
                else "unknown_action"
            )
            skipped.append({"change": change, "reason": reason})
            continue

        try:
            if action == "rename":
                new_name = str(change.get("new_name") or "").strip()[:255] or None
                if not new_name:
                    skipped.append({"change": change, "reason": "empty_name"})
                    continue

            elif action == "set_description":
                new_description = str(change.get("new_description") or "").strip()[:2000]

            elif action == "set_public":
                new_public = bool(change.get("is_public"))

            elif action == "set_dashboard_params":
                params = change.get("params")
                if not isinstance(params, list):
                    skipped.append({"change": change, "reason": "params_must_be_list"})
                    continue
                new_dashboard_params = params

            elif action == "set_time_range":
                tr = change.get("time_range")
                if tr is not None and not isinstance(tr, dict):
                    skipped.append({"change": change, "reason": "time_range_must_be_object"})
                    continue
                new_time_range = tr or {}

            elif action == "remove_chart":
                item_id = str(change.get("item_id") or "")
                item = item_by_id.get(item_id)
                if item is None:
                    skipped.append({"change": change, "reason": "unknown_item_id"})
                    continue
                await item_repo.delete(item)
                item_by_id.pop(item_id, None)

            elif action == "add_chart":
                if connection_id_str is None:
                    skipped.append({"change": change, "reason": "no_connection_on_dashboard"})
                    continue
                spec = change.get("spec") or {}
                sql = str(spec.get("sql", "")).strip()
                if not _is_select_only(sql):
                    skipped.append({"change": change, "reason": "unsafe_sql"})
                    continue
                safe_sql = _inject_limit(sql, 5000, dialect=dialect)
                q = await query_repo.create(
                    org_id,
                    current_user.id,
                    name=str(spec.get("query_name", "Query")),
                    description=str(spec.get("description", "")),
                    sql_text=safe_sql,
                    connection_id=UUID(connection_id_str),
                    params=spec.get("params") or [],
                )
                viz = await viz_repo.create(
                    org_id,
                    q.id,
                    current_user.id,
                    name=str(spec.get("query_name", "Chart")),
                    chart_type=str(spec.get("chart_type", "table")),
                    config={k: v for k, v in (spec.get("config") or {}).items() if v is not None},
                )
                new_item = await item_repo.create(
                    org_id, dashboard_id, viz.id, next_position,
                )
                next_position += 1
                item_by_id[str(new_item.id)] = new_item

            elif action == "replace_chart":
                item_id = str(change.get("item_id") or "")
                item = item_by_id.get(item_id)
                if item is None or item.visualization_id is None:
                    skipped.append({"change": change, "reason": "unknown_item_id"})
                    continue
                spec = change.get("spec") or {}
                sql = str(spec.get("sql", "")).strip()
                if not _is_select_only(sql):
                    skipped.append({"change": change, "reason": "unsafe_sql"})
                    continue
                safe_sql = _inject_limit(sql, 5000, dialect=dialect)
                viz = await viz_repo.get_by_id_and_org(item.visualization_id, org_id)
                if viz is None:
                    skipped.append({"change": change, "reason": "viz_missing"})
                    continue
                q = await query_repo.get_by_id_and_org(viz.query_id, org_id)
                if q is None:
                    skipped.append({"change": change, "reason": "query_missing"})
                    continue
                q.sql_text = safe_sql
                if spec.get("query_name"):
                    q.name = str(spec["query_name"])[:255]
                if isinstance(spec.get("params"), list):
                    q.params = spec["params"]
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

    update_fields: dict[str, Any] = {}
    if new_name is not None:
        update_fields["name"] = new_name
    if new_description is not None:
        update_fields["description"] = new_description
    if new_dashboard_params is not None:
        update_fields["dashboard_params"] = new_dashboard_params
    if new_time_range is not None:
        update_fields["time_range"] = new_time_range
    if new_public is not None:
        update_fields["is_public"] = new_public
    if update_fields:
        await dash_repo.update(dashboard, **update_fields)

    await db.commit()

    try:
        await log_audit(
            db,
            org_id=org_id,
            user_id=current_user.id,
            action="studio.agent_iterate_dashboard",
            metadata={
                "dashboard_id": str(dashboard_id),
                "applied": len(applied),
                "skipped": len(skipped),
            },
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
