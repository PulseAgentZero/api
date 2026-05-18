"""Conversational agent service with live-data tools."""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from uuid import UUID

from anthropic import AsyncAnthropic
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Conversational memory configuration
_MEM_AGENT_NAME = "conversational"
_MEM_SCOPE = "user"
_MEM_KEY = "profile"
_MEM_TOP_N = 5
_MEM_MAX_ENTITIES = 50
_MEM_MAX_TOPICS = 30
_MEM_ENTITY_RE = re.compile(r"\b[A-Z]{2,}-?\d{2,}\b")
_MEM_TOPIC_KEYWORDS = (
    "recommendation", "critical", "high risk", "churn", "overview",
    "draft", "similar", "metrics", "trend", "anomaly", "summary",
)

from app.config.settings import settings
from app.infrastructure.database.client_queries import (
    ClientDBError,
    compute_risk,
    fetch_entities,
    fetch_entity_by_id,
    get_schema_mapping,
)
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.agent_memory_repository import (
    AgentMemoryRepository,
)
from app.infrastructure.database.repositories.organization_repository import (
    OrganizationRepository,
)
from app.infrastructure.database.repositories.recommendation_repository import (
    RecommendationRepository,
)
from app.services.conversational_agents import run_split, synthesis_agent_run
from app.services.conversational_memory import (
    format_handoff_for_prompt,
    format_recalled_for_prompt,
    recall as recall_memories,
    reflect_and_commit,
)
from groq import AsyncGroq

from app.agents.prompts.conversational import (
    CLARIFICATION_REPLY_PROMPT,
    GREETING_REPLY_PROMPT,
    HELP_REPLY_PROMPT,
    OFF_TOPIC_REPLY_PROMPT,
    render_chat_system_prompt,
)
from app.services.intent_router import (
    CONVERSATIONAL_INTENTS,
    build_fastpath_args,
    classify_intent,
    filter_tools_by_intent,
)


_conv_groq_client: AsyncGroq | None = None


def _get_conv_groq() -> AsyncGroq | None:
    global _conv_groq_client
    if not settings.is_groq_configured():
        return None
    if _conv_groq_client is None:
        _conv_groq_client = AsyncGroq(
            api_key=settings.groq_api_key, max_retries=1, timeout=6.0,
        )
    return _conv_groq_client


_CONV_REPLY_PROMPTS = {
    "greeting": GREETING_REPLY_PROMPT,
    "help": HELP_REPLY_PROMPT,
    "off_topic": OFF_TOPIC_REPLY_PROMPT,
    "unknown": CLARIFICATION_REPLY_PROMPT,
}


async def _craft_conversational_reply(intent_name: str, state: dict) -> str | None:
    """Groq fast-model call to craft a warm, grounded reply for a conversational intent.

    Returns the reply text on success, or None if Groq is unavailable / errors out
    / returns an unparseable response — caller falls back to a static template."""
    system = _CONV_REPLY_PROMPTS.get(intent_name)
    if system is None:
        return None
    client = _get_conv_groq()
    if client is None:
        return None
    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=settings.GROQ_LLM_MODEL_FAST,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(state, default=str)[:1200]},
                ],
                temperature=0.6,
                max_tokens=300,
                response_format={"type": "json_object"},
            ),
            timeout=6.0,
        )
        raw = (response.choices[0].message.content or "").strip()
        data = json.loads(raw)
        reply = (data.get("reply") or "").strip() if isinstance(data, dict) else ""
        return reply or None
    except Exception as exc:
        logger.debug("[conv_reply] LLM craft failed (%s), falling back to template", exc)
        return None


TOOLS = [
    {
        "name": "get_overview",
        "description": "Get total entities, risk breakdown, active recommendation count, and top at-risk entities.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_entities",
        "description": "Get filtered entity summaries with risk scores.",
        "input_schema": {
            "type": "object",
            "properties": {
                "risk_tier": {"type": "string"},
                "search": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_entity_detail",
        "description": "Get a full live profile and active recommendations for one entity.",
        "input_schema": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_recommendations",
        "description": "Get active recommendations filtered by urgency.",
        "input_schema": {
            "type": "object",
            "properties": {
                "urgency": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "generate_action_draft",
        "description": "Generate a concise action draft for one entity using its live profile.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "action_type": {"type": "string"},
            },
            "required": ["entity_id", "action_type"],
            "additionalProperties": False,
        },
    },
    {
        "name": "find_similar_entities",
        "description": "Find entities similar to a given entity by behaviour profile. Returns ranked list with similarity scores.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "Entity ID to find similar entities for"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["entity_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "build_custom_dashboard",
        "description": (
            "Build a complete Pulse Studio dashboard from a natural language goal. "
            "Introspects the client database schema, generates SQL queries, picks chart types, "
            "creates visualizations, and returns a link to the new dashboard. "
            "Use when the user asks to 'build a dashboard', 'create charts', "
            "'show me X visually', 'make a report on Y', or 'visualise Z'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "Natural language description of what the dashboard should show",
                },
                "dashboard_name": {
                    "type": "string",
                    "description": "Name for the new dashboard (optional, defaults to goal summary)",
                },
                "is_public": {
                    "type": "boolean",
                    "description": "Whether to make the dashboard publicly shareable via a link. Default false.",
                },
                "max_charts": {
                    "type": "integer",
                    "description": "Maximum number of charts to generate (1–6). Default 4.",
                    "minimum": 1,
                    "maximum": 6,
                },
            },
            "required": ["goal"],
            "additionalProperties": False,
        },
    },
]


def _json_ready(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


async def _overview(db: AsyncSession, org_id: UUID) -> dict:
    mapping = await get_schema_mapping(db, org_id)
    entities = await fetch_entities(db, org_id, mapping)
    entities = compute_risk(entities, mapping.signal_columns, mapping.risk_config)
    breakdown = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for entity in entities:
        breakdown[entity["risk_tier"]] += 1
    top = sorted(entities, key=lambda item: item["risk_score"], reverse=True)[:3]
    active_recs = await RecommendationRepository(db).list_by_org(org_id, status="open")
    return {
        "total_entities": len(entities),
        "risk_breakdown": breakdown,
        "active_recommendations": len(active_recs),
        "top_at_risk": [
            {
                "entity_id": entity[mapping.entity_id_col],
                "entity_label": entity.get(mapping.entity_name_col) if mapping.entity_name_col else None,
                "risk_score": entity["risk_score"],
                "risk_tier": entity["risk_tier"],
            }
            for entity in top
        ],
    }


async def _entities(
    db: AsyncSession,
    org_id: UUID,
    risk_tier: str | None = None,
    search: str | None = None,
    limit: int = 25,
) -> dict:
    mapping = await get_schema_mapping(db, org_id)
    entities = await fetch_entities(db, org_id, mapping)
    entities = compute_risk(entities, mapping.signal_columns, mapping.risk_config)
    id_col = mapping.entity_id_col
    name_col = mapping.entity_name_col

    rows = []
    for entity in entities:
        label = entity.get(name_col) if name_col else None
        if risk_tier and entity["risk_tier"] != risk_tier:
            continue
        if search and label and search.lower() not in str(label).lower():
            continue
        rows.append(
            {
                "entity_id": entity[id_col],
                "entity_label": label,
                "risk_score": entity["risk_score"],
                "risk_tier": entity["risk_tier"],
                "signals": entity.get("signals", {}),
            }
        )

    rows = sorted(rows, key=lambda item: item["risk_score"], reverse=True)
    return {"entities": rows[:limit], "total": len(rows)}


async def _entity_detail(db: AsyncSession, org_id: UUID, entity_id: str) -> dict:
    mapping = await get_schema_mapping(db, org_id)
    entity = await fetch_entity_by_id(db, org_id, entity_id, mapping)
    if entity is None:
        return {"error": "Entity not found"}
    entity = compute_risk([entity], mapping.signal_columns, mapping.risk_config)[0]
    recs = await RecommendationRepository(db).list_by_org(org_id, status="open")
    return {
        "entity_id": entity[mapping.entity_id_col],
        "entity_label": entity.get(mapping.entity_name_col) if mapping.entity_name_col else None,
        "risk_score": entity["risk_score"],
        "risk_tier": entity["risk_tier"],
        "signals": entity.get("signals", {}),
        "fields": {k: v for k, v in entity.items() if k not in ("risk_score", "risk_tier", "signals")},
        "active_recommendations": [
            {
                "id": rec.id,
                "urgency": rec.urgency,
                "title": rec.title,
                "reasoning": rec.reasoning,
                "suggested_action": rec.suggested_action,
            }
            for rec in recs
            if rec.entity_id == entity_id
        ],
    }


async def _recommendations(
    db: AsyncSession,
    org_id: UUID,
    urgency: str | None = None,
    limit: int = 25,
) -> dict:
    recs = await RecommendationRepository(db).list_by_org(
        org_id, urgency=urgency, status="open"
    )
    return {
        "recommendations": [
            {
                "id": rec.id,
                "entity_id": rec.entity_id,
                "entity_label": rec.entity_label,
                "urgency": rec.urgency,
                "title": rec.title,
                "reasoning": rec.reasoning,
                "suggested_action": rec.suggested_action,
            }
            for rec in recs[:limit]
        ],
        "total": len(recs),
    }


async def _find_similar(
    db: AsyncSession,
    org_id: UUID,
    entity_id: str,
    limit: int = 10,
) -> dict:
    """Semantic search for entities similar to the given entity_id."""
    from app.infrastructure.external_services.embeddings import embedding_service
    from app.infrastructure.external_services.qdrant import QdrantService

    if not settings.is_voyage_configured():
        return {"error": "Vector search not available", "similar_entities": []}

    mapping = await get_schema_mapping(db, org_id)
    entity = await fetch_entity_by_id(db, org_id, entity_id, mapping)
    if entity is None:
        return {"error": "Entity not found"}

    signals = {
        sig_label: entity.get(col_name)
        for sig_label, col_name in (mapping.signal_columns or {}).items()
        if col_name in entity
    }
    query_text = f"Entity {entity_id}: signals {json.dumps(signals, default=str)}"

    try:
        qdrant = QdrantService()
        await qdrant.ensure_collection(str(org_id))
        vector = await embedding_service.embed_query(query_text)
        results = await qdrant.search_similar(str(org_id), vector, limit=limit + 1)
    except Exception as exc:
        return {"error": f"Vector search failed: {exc}", "similar_entities": []}

    similar = [
        {
            "entity_id": r.entity_id,
            "similarity": round(float(r.score), 4),
            "profile_summary": r.payload.get("profile_summary", ""),
        }
        for r in results
        if str(r.entity_id) != str(entity_id)
    ][:limit]

    return {
        "query_entity": entity_id,
        "similar_entities": similar,
        "total": len(similar),
    }


from app.agents.prompts.action_draft import ACTION_DRAFT_PROMPT as _ACTION_DRAFT_SYSTEM


def _action_draft_fallback(detail: dict, entity_id: str, action_type: str) -> dict:
    """Template draft used when the LLM is unconfigured or the call fails."""
    label = detail.get("entity_label") or entity_id
    signals = detail.get("signals") or {}
    numeric_signals = {k: v for k, v in signals.items() if isinstance(v, (int, float))}
    top_signal = max(numeric_signals, key=lambda key: numeric_signals[key]) if numeric_signals else None
    if action_type == "message":
        draft = (
            f"Hi {label}, we noticed changes in your account experience and want to help. "
            "Our team can review your current plan and offer the most relevant support option today."
        )
    else:
        draft = (
            f"Action plan for {label}: review the live profile, prioritize the {top_signal or 'highest'} "
            "risk signal, contact the entity, and mark the recommendation as actioned after intervention."
        )
    return {"entity_id": entity_id, "action_type": action_type, "draft": draft}


async def _action_draft(
    db: AsyncSession,
    org_id: UUID,
    entity_id: str,
    action_type: str,
) -> dict:
    detail = await _entity_detail(db, org_id, entity_id)
    if detail.get("error"):
        return detail

    if not settings.is_anthropic_configured():
        return _action_draft_fallback(detail, entity_id, action_type)

    org = await OrganizationRepository(db).get_by_id(org_id)
    org_name = org.name if org else "the team"
    entity_label_name = org.entity_label if org and org.entity_label else "entity"
    goal_label = org.goal_label if org and org.goal_label else "improve outcomes"
    business_context = (org.business_context if org else "") or ""

    label = detail.get("entity_label") or entity_id
    signals = detail.get("signals") or {}
    numeric = [(k, v) for k, v in signals.items() if isinstance(v, (int, float))]
    top_signals = sorted(numeric, key=lambda kv: abs(kv[1]), reverse=True)[:5]
    recs = detail.get("active_recommendations") or []

    parts = [
        f"Org: {org_name}. Entity type: {entity_label_name}. Goal: {goal_label}.",
    ]
    if business_context.strip():
        parts.append(f"Business context: {business_context[:300]}")
    parts.append(f"Entity: {label} (id={entity_id})")
    parts.append(
        f"Risk: tier={detail.get('risk_tier')}, score={detail.get('risk_score')}"
    )
    if top_signals:
        parts.append("Top signals: " + ", ".join(f"{k}={v}" for k, v in top_signals))
    if recs:
        first = recs[0]
        parts.append(
            f"Top recommendation: {first.get('title')} — {(first.get('reasoning') or '')[:200]}"
        )
    parts.append(
        f"\nDraft a {action_type} (under 100 words, professional, specific to the signals above)."
    )
    user_msg = "\n".join(parts)

    try:
        client = AsyncAnthropic(api_key=settings.get_anthropic_api_key())
        resp = await client.messages.create(
            model=settings.ANTHROPIC_LLM_MODEL,
            max_tokens=400,
            system=_ACTION_DRAFT_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        draft = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not draft:
            return _action_draft_fallback(detail, entity_id, action_type)
        return {"entity_id": entity_id, "action_type": action_type, "draft": draft}
    except Exception as exc:
        logger.warning("[action_draft] LLM call failed, using template: %s", exc)
        return _action_draft_fallback(detail, entity_id, action_type)


async def _build_custom_dashboard(
    db: AsyncSession,
    org_id: UUID,
    current_user: User,
    goal: str,
    dashboard_name: str | None,
    is_public: bool,
    max_charts: int,
) -> dict:
    """Build a Pulse Studio dashboard from a natural language goal.

    Steps:
    1. Introspect the client database schema
    2. Ask the LLM to generate SQL queries + chart configs for the goal
    3. Persist queries → visualizations → dashboard → items
    4. Return dashboard link
    """
    import re as _re
    import secrets as _secrets

    from app.agents.tools.client_db import open_client_engine, safe_client_connection
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
    from app.agents.tools.client_db import schema_columns_sql

    max_charts = max(1, min(max_charts, 6))

    # ── Step 1: Introspect schema ────────────────────────────────────────────
    schema_context = ""
    try:
        engine, conn = await open_client_engine(db, org_id)
        try:
            async with safe_client_connection(engine, conn) as client_conn:
                from sqlalchemy import text as _text

                db_type = getattr(conn, "db_type", None)
                # List tables
                if db_type == "mysql":
                    tables_sql = "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE() LIMIT 20"
                elif db_type == "mssql":
                    tables_sql = "SELECT TOP 20 TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE='BASE TABLE'"
                elif db_type == "sqlite":
                    tables_sql = "SELECT name FROM sqlite_master WHERE type='table' LIMIT 20"
                else:
                    tables_sql = "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() AND table_type = 'BASE TABLE' LIMIT 20"

                table_rows = (await client_conn.execute(_text(tables_sql))).all()
                table_names = [r[0] for r in table_rows]

                cols_sql = schema_columns_sql(db_type)
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
                schema_context = "\n".join(lines)
        finally:
            await engine.dispose()
    except Exception as exc:
        return {"error": f"Could not connect to client database: {exc}"}

    if not schema_context:
        return {"error": "No tables found in client database"}

    # ── Step 2: LLM planning ─────────────────────────────────────────────────
    system_prompt = (
        "You are a SQL expert. Given a database schema and a goal, generate a JSON array "
        "of chart specifications. Each spec must have: "
        "\"query_name\" (string), \"description\" (string), \"sql\" (valid SELECT SQL), "
        "\"chart_type\" (one of: bar, line, area, pie, scatter, table, number), "
        "\"config\" (object with relevant fields: x_axis, y_axis, color, title, value_column, label_column). "
        "Output ONLY valid JSON. No markdown. No explanation."
    )
    user_prompt = (
        f"Schema:\n{schema_context}\n\n"
        f"Goal: {goal}\n\n"
        f"Generate {max_charts} chart specifications."
    )

    try:
        from app.infrastructure.llm.factory import get_llm_client
        llm = get_llm_client()
        if not llm.is_configured():
            return {"error": f"AI provider '{llm.provider_name}' is not configured"}
        raw_text = await llm.complete(system_prompt, user_prompt, max_tokens=2000, temperature=0.1)
        plan: list[dict] = json.loads(raw_text)
        if not isinstance(plan, list):
            raise ValueError("Expected JSON array")
    except Exception as exc:
        logger.warning("[build_dashboard] LLM planning failed: %s", exc)
        return {"error": "Agent could not generate a dashboard plan from the goal"}

    # Filter out unsafe SQL
    safe_plan = [
        spec for spec in plan
        if isinstance(spec, dict) and spec.get("sql") and _is_select_only(str(spec["sql"]))
    ]
    if not safe_plan:
        return {"error": "Generated SQL was not safe to execute — only SELECT statements are allowed"}

    # ── Step 3: Persist ──────────────────────────────────────────────────────
    query_repo = StudioQueryRepository(db)
    viz_repo = StudioVisualizationRepository(db)
    dash_repo = StudioDashboardRepository(db)
    item_repo = StudioDashboardItemRepository(db)

    created_vizs = []
    for spec in safe_plan:
        safe_sql = _inject_limit(str(spec["sql"]).strip(), 5000)
        q = await query_repo.create(
            org_id,
            current_user.id,
            name=str(spec.get("query_name", "Query")),
            description=str(spec.get("description", "")),
            sql_text=safe_sql,
            connection_id=None,
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

    # Generate slug if public
    slug: str | None = None
    if is_public:
        name_for_slug = dashboard_name or goal
        base = _re.sub(r"[^a-z0-9]+", "-", name_for_slug.lower()).strip("-")[:60]
        for _ in range(5):
            candidate = f"{base}-{_secrets.token_hex(2)}"
            if not await dash_repo.slug_exists(candidate):
                slug = candidate
                break

    dashboard_name_final = dashboard_name or goal[:80]
    dashboard = await dash_repo.create(
        org_id,
        current_user.id,
        name=dashboard_name_final,
        description=f"Auto-generated from goal: {goal[:200]}",
        is_public=is_public,
        slug=slug,
        layout=[],
    )

    layout = []
    for i, viz in enumerate(created_vizs):
        item = await item_repo.create(org_id, dashboard.id, viz.id, i)
        layout.append({
            "item_id": str(item.id),
            "x": (i % 2) * 6,
            "y": (i // 2) * 4,
            "w": 6,
            "h": 4,
        })

    await dash_repo.update(dashboard, layout=layout)
    await db.commit()

    # ── Step 4: Audit + return ───────────────────────────────────────────────
    try:
        await log_audit(
            db,
            org_id=org_id,
            user_id=current_user.id,
            action="studio.agent_build_dashboard",
            resource="studio_dashboard",
            resource_id=dashboard.id,
            metadata={"goal": goal, "chart_count": len(created_vizs)},
        )
    except Exception:
        pass

    internal_url = f"/studio/dashboards/{dashboard.id}"
    public_url = f"/api/public/v1/studio/dashboards/{slug}" if slug else None

    return {
        "dashboard_id": str(dashboard.id),
        "dashboard_name": dashboard.name,
        "chart_count": len(created_vizs),
        "internal_url": internal_url,
        "public_url": public_url,
        "charts": [
            {
                "query_name": spec.get("query_name", "Chart"),
                "chart_type": spec.get("chart_type", "table"),
                "visualization_id": str(viz.id),
            }
            for spec, viz in zip(safe_plan, created_vizs)
        ],
        "message": (
            f"Dashboard '{dashboard.name}' created with {len(created_vizs)} chart(s). "
            + (f"Shareable at: {public_url}" if public_url else "Dashboard is private.")
        ),
    }


async def _run_tool(
    name: str,
    tool_input: dict,
    db: AsyncSession,
    org_id: UUID,
    *,
    current_user: User | None = None,
) -> dict:
    try:
        if name == "get_overview":
            return await _overview(db, org_id)
        if name == "get_entities":
            return await _entities(
                db,
                org_id,
                risk_tier=tool_input.get("risk_tier"),
                search=tool_input.get("search"),
                limit=int(tool_input.get("limit") or 25),
            )
        if name == "get_entity_detail":
            return await _entity_detail(db, org_id, str(tool_input["entity_id"]))
        if name == "get_recommendations":
            return await _recommendations(
                db,
                org_id,
                urgency=tool_input.get("urgency"),
                limit=int(tool_input.get("limit") or 25),
            )
        if name == "generate_action_draft":
            return await _action_draft(
                db,
                org_id,
                str(tool_input["entity_id"]),
                str(tool_input["action_type"]),
            )
        if name == "find_similar_entities":
            return await _find_similar(
                db,
                org_id,
                str(tool_input["entity_id"]),
                limit=int(tool_input.get("limit") or 10),
            )
        if name == "build_custom_dashboard":
            if current_user is None:
                return {"error": "build_custom_dashboard requires an authenticated user"}
            return await _build_custom_dashboard(
                db,
                org_id,
                current_user,
                goal=str(tool_input["goal"]),
                dashboard_name=tool_input.get("dashboard_name"),
                is_public=bool(tool_input.get("is_public", False)),
                max_charts=int(tool_input.get("max_charts") or 4),
            )
    except ClientDBError as exc:
        return {"error": str(exc)}
    return {"error": f"Unknown tool: {name}"}


async def _load_user_memory(db: AsyncSession, current_user: User) -> dict:
    """Read this user's accumulated chat profile from agent_memory (best-effort)."""
    try:
        repo = AgentMemoryRepository(db)
        record = await repo.get_scoped(
            current_user.org_id, _MEM_SCOPE, current_user.id, _MEM_AGENT_NAME, key=_MEM_KEY,
        )
        return (record.data if record else {}) or {}
    except Exception as exc:
        logger.debug("[memory] load failed (non-fatal): %s", exc)
        return {}


def _format_memory_for_prompt(memory: dict) -> str:
    if not memory:
        return ""
    parts: list[str] = []
    freq_entities = memory.get("frequent_entities") or {}
    if freq_entities:
        top = sorted(freq_entities.items(), key=lambda kv: kv[1], reverse=True)[:_MEM_TOP_N]
        parts.append(
            "Entities the user has asked about most: "
            + ", ".join(f"{eid} ({n}x)" for eid, n in top)
        )
    freq_topics = memory.get("frequent_topics") or {}
    if freq_topics:
        top = sorted(freq_topics.items(), key=lambda kv: kv[1], reverse=True)[:_MEM_TOP_N]
        parts.append(
            "Recurring topics in the user's questions: "
            + ", ".join(f"{kw} ({n}x)" for kw, n in top)
        )
    if memory.get("total_turns"):
        parts.append(f"Total prior turns with this user: {memory['total_turns']}.")
    if not parts:
        return ""
    return "User memory (use to bias recommendations and pre-load relevant context):\n- " + "\n- ".join(parts) + "\n"


async def update_user_memory_from_message(
    db: AsyncSession, current_user: User, user_message: str
) -> None:
    """Heuristic per-turn update: count entity IDs and topic keywords mentioned by the user."""
    if not user_message:
        return
    repo = AgentMemoryRepository(db)
    try:
        record = await repo.get_scoped(
            current_user.org_id, _MEM_SCOPE, current_user.id, _MEM_AGENT_NAME, key=_MEM_KEY,
        )
        data = (record.data if record else {}) or {}

        freq_entities: dict[str, int] = dict(data.get("frequent_entities") or {})
        for match in _MEM_ENTITY_RE.findall(user_message):
            freq_entities[match] = freq_entities.get(match, 0) + 1
        if len(freq_entities) > _MEM_MAX_ENTITIES:
            freq_entities = dict(
                sorted(freq_entities.items(), key=lambda kv: kv[1], reverse=True)[:_MEM_MAX_ENTITIES]
            )

        freq_topics: dict[str, int] = dict(data.get("frequent_topics") or {})
        lowered = user_message.lower()
        for kw in _MEM_TOPIC_KEYWORDS:
            if kw in lowered:
                freq_topics[kw] = freq_topics.get(kw, 0) + 1
        if len(freq_topics) > _MEM_MAX_TOPICS:
            freq_topics = dict(
                sorted(freq_topics.items(), key=lambda kv: kv[1], reverse=True)[:_MEM_MAX_TOPICS]
            )

        new_data = {
            "frequent_entities": freq_entities,
            "frequent_topics": freq_topics,
            "total_turns": int(data.get("total_turns") or 0) + 1,
            "last_active": datetime.now(timezone.utc).isoformat(),
        }
        await repo.upsert_scoped(
            current_user.org_id, _MEM_SCOPE, current_user.id, _MEM_AGENT_NAME,
            data=new_data, key=_MEM_KEY,
        )
    except Exception as exc:
        logger.debug("[memory] upsert failed (non-fatal): %s", exc)


def _latest_user_message(conversation_messages: list[dict]) -> str:
    for msg in reversed(conversation_messages):
        if msg.get("role") == "user" and msg.get("content"):
            content = msg["content"]
            if isinstance(content, str):
                return content
    return ""


async def _system_prompt(
    db: AsyncSession,
    current_user: User,
    *,
    recalled_block: str = "",
    handoff_block: str = "",
) -> str:
    org = await OrganizationRepository(db).get_by_id(current_user.org_id)
    org_name = org.name if org else "this organization"
    entity_label = org.entity_label if org and org.entity_label else "entities"
    goal_label = org.goal_label if org and org.goal_label else "improve operations"
    context = org.business_context if org and org.business_context else "No business context configured."

    pipeline_block = await _pipeline_context_block(db, current_user.org_id)
    memory = await _load_user_memory(db, current_user)
    memory_block = _format_memory_for_prompt(memory)

    return render_chat_system_prompt(
        org_name=org_name,
        entity_label=entity_label,
        goal_label=goal_label,
        business_context=context,
        pipeline_block=pipeline_block,
        memory_block=memory_block,
        handoff_block=handoff_block,
        recalled_block=recalled_block,
    )


async def _pipeline_context_block(db: AsyncSession, org_id: UUID) -> str:
    """Compose a short autonomous-pipeline status section for the system prompt."""
    from app.infrastructure.database.repositories.pipeline_run_repository import (
        PipelineRunRepository,
    )

    repo = PipelineRunRepository(db)
    try:
        recent = await repo.list_by_org(org_id, limit=5)
    except Exception:
        return ""

    if not recent:
        return (
            "Autonomous pipeline status: no pipeline run has completed yet for "
            "this organization. If asked about the latest analysis, say so and "
            "fall back to live tool calls.\n"
        )

    active = next((r for r in recent if r.status in ("queued", "running")), None)
    last_done = next((r for r in recent if r.status in ("succeeded", "failed")), None)

    lines = ["Autonomous pipeline status:"]
    if active is not None:
        lines.append(
            f"- A pipeline run is currently {active.status} "
            f"(step '{active.current_step or 'unknown'}', id={active.id})."
        )
    if last_done is not None:
        ts = last_done.completed_at.isoformat() if last_done.completed_at else "unknown time"
        if last_done.status == "succeeded":
            lines.append(
                f"- Last successful run completed at {ts}: "
                f"{last_done.entities_scored} entities scored "
                f"({last_done.critical_count} critical, {last_done.high_count} high), "
                f"{last_done.recommendations_generated} recommendations generated."
            )
        else:
            lines.append(
                f"- Last run failed at {ts}: {last_done.error or 'unknown error'}."
            )
    if active is None and last_done is None:
        lines.append("- No completed runs available.")

    lines.append(
        "Treat these numbers as the latest persisted snapshot; if the user asks "
        "for live numbers, use the tools to re-query the client database."
    )
    return "\n".join(lines) + "\n"


async def _fallback_reply(
    db: AsyncSession,
    current_user: User,
    conversation_messages: list[dict],
) -> str:
    message = conversation_messages[-1]["content"] if conversation_messages else ""
    lowered = message.lower()
    entity_match = re.search(r"\b[A-Z]{2,}-?\d{2,}\b", message)

    try:
        if "recommend" in lowered:
            data = await _recommendations(db, current_user.org_id, limit=5)
            return "Active recommendations: " + json.dumps(_json_ready(data), ensure_ascii=False)
        if entity_match:
            entity_id = entity_match.group(0)
            if "draft" in lowered or "message" in lowered:
                data = await _action_draft(db, current_user.org_id, entity_id, "message")
            else:
                data = await _entity_detail(db, current_user.org_id, entity_id)
            return json.dumps(_json_ready(data), ensure_ascii=False)

        data = await _overview(db, current_user.org_id)
    except ClientDBError as exc:
        return f"I cannot answer from live data yet: {exc}"

    return (
        f"Current overview: {data['total_entities']} total entities, "
        f"{data['risk_breakdown']['critical']} critical, "
        f"{data['risk_breakdown']['high']} high risk, and "
        f"{data['active_recommendations']} active recommendations."
    )


async def _conversational_reply(
    db: AsyncSession,
    current_user: User,
    intent_name: str,
    user_message: str,
) -> str:
    """Tool-free reply for greeting / help / off_topic / unknown.

    Pulls org context (name, entity_label, goal_label) so the reply is grounded
    in the org's vocabulary instead of generic Pulse boilerplate.
    """
    try:
        org = await OrganizationRepository(db).get_by_id(current_user.org_id)
    except Exception:
        org = None
    org_name = (org.name if org else None) or "your team"
    entity_label = (org.entity_label if org and org.entity_label else "entities").lower()
    goal_label = (org.goal_label if org and org.goal_label else "improve operations").lower()

    user_first = ""
    try:
        full = getattr(current_user, "full_name", None) or getattr(current_user, "name", None) or ""
        user_first = str(full).split()[0] if full else ""
    except Exception:
        user_first = ""

    # State passed to the LLM prompt and the fallback templates.
    state = {
        "user_first": user_first,
        "org_name": org_name,
        "entity_label": entity_label,
        "goal_label": goal_label,
        "message": user_message,
    }

    # Try LLM-crafted reply first (warmer + tone-matched).
    crafted = await _craft_conversational_reply(intent_name, state)
    if crafted:
        return crafted

    # Fallback: static templates when Groq is unavailable or returns junk.
    return _conversational_reply_template(intent_name, state)


def _conversational_reply_template(intent_name: str, state: dict) -> str:
    """Deterministic template fallback for the conversational intents."""
    user_first = state.get("user_first") or ""
    org_name = state.get("org_name") or "your team"
    entity_label = state.get("entity_label") or "entities"
    goal_label = state.get("goal_label") or "improve operations"
    singular = entity_label[:-1] if entity_label.endswith("s") else entity_label

    if intent_name == "greeting":
        opener = f"Hi {user_first}!" if user_first else "Hi!"
        return (
            f"{opener} I'm Pulse, the operational intelligence agent for {org_name}. "
            f"Ask me about your {entity_label}, risk tiers, recommendations, or to "
            f"draft an outreach: anything that helps you {goal_label}."
        )

    if intent_name == "help":
        return (
            f"Here's what I can do for {org_name}:\n"
            f"- Quick overview (\"what's our status?\")\n"
            f"- Pull details for a {singular} (\"tell me about NG-00075\")\n"
            f"- List {entity_label} by tier (\"show critical {entity_label}\")\n"
            f"- Active recommendations (\"what should I action today?\")\n"
            f"- Find similar {entity_label} (\"5 more like NG-00075\")\n"
            f"- Draft an outreach (\"draft a message for NG-00075\")\n"
            f"- Explain trends (\"why is NG-00075 critical?\")\n\n"
            f"What would you like to start with?"
        )

    if intent_name == "off_topic":
        return (
            f"That's outside what I can help with. I'm focused on {org_name}'s "
            f"{entity_label}: try \"what's our status?\" or \"show critical {entity_label}\" to start."
        )

    # unknown / clarification
    return (
        f"I'm not sure what you're after. Try \"what's our status?\", "
        f"\"show critical {entity_label}\", or \"what should I action today?\" to get going."
    )


async def run(
    db: AsyncSession,
    current_user: User,
    conversation_messages: list[dict],
) -> str:
    """Process the conversation using Claude tool calls when configured."""

    if not settings.is_anthropic_configured():
        return await _fallback_reply(db, current_user, conversation_messages)

    latest_user = _latest_user_message(conversation_messages)
    recalled = await recall_memories(current_user, latest_user) if latest_user else []
    recalled_block = format_recalled_for_prompt(recalled)

    # First-turn handoff: surface prior-session summaries only on conversation entry,
    # so reopened threads aren't polluted by mid-thread handoff text.
    handoff_block = ""
    is_first_turn = len([m for m in conversation_messages if m.get("role") == "user"]) <= 1
    if is_first_turn and latest_user:
        handoffs = await recall_memories(
            current_user, latest_user,
            top_k=settings.CONV_MEMORY_HANDOFF_K,
            kind="conversation_summary",
            min_score=0.0,  # any prior summary is worth surfacing on first turn
        )
        handoff_block = format_handoff_for_prompt(handoffs)

    # Semantic intent detection: fast-path high-confidence simple intents past
    # the full ReAct loop; otherwise prefilter the tool list for the ReAct loop.
    tools_for_run = TOOLS
    if settings.CHAT_INTENT_DETECTION_ENABLED and latest_user:
        recent_for_intent = [
            {"role": m["role"], "content": m["content"]}
            for m in conversation_messages[-6:]
            if m.get("role") in {"user", "assistant"} and isinstance(m.get("content"), str)
        ]
        intent = await classify_intent(latest_user, convo_history=recent_for_intent)

        # Conversational intents — greeting / help / off_topic / (low-confidence) unknown.
        # Skip ReAct and tool calls entirely; return a context-aware reply.
        if intent and intent.intent in CONVERSATIONAL_INTENTS and intent.confidence >= 0.5:
            logger.info(
                "[agent_service] conversational intent=%s confidence=%.2f",
                intent.intent, intent.confidence,
            )
            return await _conversational_reply(db, current_user, intent.intent, latest_user)

        if intent and intent.confidence >= settings.CHAT_INTENT_FASTPATH_CONFIDENCE:
            fp = build_fastpath_args(intent)
            if fp is not None:
                tool_name, tool_args = fp
                logger.info(
                    "[agent_service] intent fast-path: intent=%s confidence=%.2f tool=%s",
                    intent.intent, intent.confidence, tool_name,
                )
                try:
                    tool_result = await _run_tool(
                        tool_name, tool_args, db, current_user.org_id,
                        current_user=current_user,
                    )
                    base_system = await _system_prompt(
                        db, current_user,
                        recalled_block=recalled_block, handoff_block=handoff_block,
                    )
                    synthesis_reply = await synthesis_agent_run(
                        db, current_user, conversation_messages,
                        {tool_name: _json_ready(tool_result)},
                        base_system_prompt=base_system,
                    )
                    if synthesis_reply:
                        try:
                            await reflect_and_commit(current_user, latest_user, synthesis_reply)
                        except Exception as exc:
                            logger.debug("[agent_service] reflect_and_commit failed: %s", exc)
                        return synthesis_reply
                except Exception as exc:
                    logger.warning(
                        "[agent_service] fast-path failed, falling back to ReAct: %s", exc,
                    )
        if intent:
            tools_for_run = filter_tools_by_intent(TOOLS, intent)
            logger.debug(
                "[agent_service] intent=%s confidence=%.2f tools=%d/%d",
                intent.intent, intent.confidence, len(tools_for_run), len(TOOLS),
            )

    # Optional Query+Synthesis split (hierarchical pattern from Agentic Architectures e-book).
    if settings.CONV_AGENT_SPLIT_ENABLED:
        base_system = await _system_prompt(
            db, current_user, recalled_block=recalled_block, handoff_block=handoff_block,
        )
        try:
            reply_text = await run_split(
                db, current_user, conversation_messages,
                base_system_prompt=base_system,
                run_tool=_run_tool, full_tools=tools_for_run, json_ready=_json_ready,
            )
        except Exception as exc:
            logger.warning("[agent_service] split run failed, falling back to single-agent: %s", exc)
            reply_text = ""
        if reply_text:
            if latest_user:
                try:
                    await reflect_and_commit(current_user, latest_user, reply_text)
                except Exception as exc:
                    logger.debug("[agent_service] reflect_and_commit failed: %s", exc)
            return reply_text
        # If split returned empty, fall through to single-agent path below.

    reply_text: str = ""
    try:
        client = AsyncAnthropic(api_key=settings.get_anthropic_api_key())
        messages = [
            {"role": msg["role"], "content": msg["content"]}
            for msg in conversation_messages
            if msg.get("role") in {"user", "assistant"} and msg.get("content")
        ]

        response = await client.messages.create(
            model=settings.ANTHROPIC_LLM_MODEL,
            max_tokens=900,
            system=await _system_prompt(db, current_user, recalled_block=recalled_block, handoff_block=handoff_block),
            tools=tools_for_run,
            messages=messages,
        )

        for _ in range(4):
            tool_uses = [block for block in response.content if block.type == "tool_use"]
            if not tool_uses:
                text = "".join(
                    block.text for block in response.content if block.type == "text"
                ).strip()
                reply_text = text or await _fallback_reply(db, current_user, conversation_messages)
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": [block.model_dump() for block in response.content],
                }
            )
            tool_results = []
            for tool_use in tool_uses:
                result = await _run_tool(
                    tool_use.name,
                    dict(tool_use.input or {}),
                    db,
                    current_user.org_id,
                    current_user=current_user,
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": json.dumps(_json_ready(result), ensure_ascii=False),
                    }
                )
            messages.append({"role": "user", "content": tool_results})
            response = await client.messages.create(
                model=settings.ANTHROPIC_LLM_MODEL,
                max_tokens=900,
                system=await _system_prompt(db, current_user, recalled_block=recalled_block, handoff_block=handoff_block),
                tools=tools_for_run,
                messages=messages,
            )
        else:
            reply_text = "I could not complete the tool workflow in time. Try narrowing the question."
    except Exception:
        reply_text = await _fallback_reply(db, current_user, conversation_messages)

    # Memory commit is best-effort; never let it block or fail the reply.
    if latest_user and reply_text:
        try:
            await reflect_and_commit(current_user, latest_user, reply_text)
        except Exception as exc:
            logger.debug("[agent_service] reflect_and_commit failed: %s", exc)

    return reply_text


async def run_stream(
    db: AsyncSession,
    current_user: User,
    conversation_messages: list[dict],
) -> AsyncIterator[str]:
    """Stream the agent's reply incrementally. Yields text chunks; tool execution pauses the stream."""

    if not settings.is_anthropic_configured():
        # No streaming path when Anthropic is not configured — yield the full fallback at once.
        yield await _fallback_reply(db, current_user, conversation_messages)
        return

    latest_user = _latest_user_message(conversation_messages)
    recalled = await recall_memories(current_user, latest_user) if latest_user else []
    recalled_block = format_recalled_for_prompt(recalled)

    handoff_block = ""
    is_first_turn = len([m for m in conversation_messages if m.get("role") == "user"]) <= 1
    if is_first_turn and latest_user:
        handoffs = await recall_memories(
            current_user, latest_user,
            top_k=settings.CONV_MEMORY_HANDOFF_K,
            kind="conversation_summary",
            min_score=0.0,
        )
        handoff_block = format_handoff_for_prompt(handoffs)

    client = AsyncAnthropic(api_key=settings.get_anthropic_api_key())
    messages = [
        {"role": msg["role"], "content": msg["content"]}
        for msg in conversation_messages
        if msg.get("role") in {"user", "assistant"} and msg.get("content")
    ]
    system_prompt = await _system_prompt(db, current_user, recalled_block=recalled_block, handoff_block=handoff_block)

    accumulated: list[str] = []
    try:
        for _ in range(4):
            tool_uses: list = []
            async with client.messages.stream(
                model=settings.ANTHROPIC_LLM_MODEL,
                max_tokens=900,
                system=system_prompt,
                tools=TOOLS,
                messages=messages,
            ) as stream:
                async for text_chunk in stream.text_stream:
                    if text_chunk:
                        accumulated.append(text_chunk)
                        yield text_chunk
                final_message = await stream.get_final_message()

            tool_uses = [b for b in final_message.content if b.type == "tool_use"]
            if not tool_uses:
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": [b.model_dump() for b in final_message.content],
                }
            )
            tool_results = []
            for tool_use in tool_uses:
                result = await _run_tool(
                    tool_use.name,
                    dict(tool_use.input or {}),
                    db,
                    current_user.org_id,
                    current_user=current_user,
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": json.dumps(_json_ready(result), ensure_ascii=False),
                    }
                )
            messages.append({"role": "user", "content": tool_results})
        else:
            limit_msg = "\n\n[Reached tool-loop limit — narrow the question.]"
            accumulated.append(limit_msg)
            yield limit_msg
    except Exception as exc:
        logger.warning("[agent_service] stream failed, falling back: %s", exc)
        fallback = await _fallback_reply(db, current_user, conversation_messages)
        accumulated.append(fallback)
        yield fallback

    full_reply = "".join(accumulated).strip()
    if latest_user and full_reply:
        try:
            await reflect_and_commit(current_user, latest_user, full_reply)
        except Exception as exc:
            logger.debug("[agent_service] reflect_and_commit failed: %s", exc)
