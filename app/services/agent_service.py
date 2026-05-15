"""Conversational agent service with live-data tools."""

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


_ACTION_DRAFT_SYSTEM = (
    "You write short, professional action drafts for an operations team to use with at-risk "
    "customers / entities. Be specific to the entity's signals and recommendations — never use "
    "generic boilerplate. Output ONLY the draft text. No preamble, no markdown headers, no JSON."
)


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


async def _run_tool(
    name: str,
    tool_input: dict,
    db: AsyncSession,
    org_id: UUID,
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


async def _system_prompt(db: AsyncSession, current_user: User) -> str:
    org = await OrganizationRepository(db).get_by_id(current_user.org_id)
    org_name = org.name if org else "this organization"
    entity_label = org.entity_label if org and org.entity_label else "entities"
    goal_label = org.goal_label if org and org.goal_label else "improve operations"
    context = org.business_context if org and org.business_context else "No business context configured."

    pipeline_block = await _pipeline_context_block(db, current_user.org_id)
    memory = await _load_user_memory(db, current_user)
    memory_block = _format_memory_for_prompt(memory)

    return (
        f"You are Pulse, an operational intelligence agent for {org_name}. "
        f"The organization models {entity_label} and the goal is to {goal_label}. "
        f"Business context: {context}\n"
        f"{pipeline_block}"
        f"{memory_block}"
        "Answer data-dependent questions only after using the provided tools. "
        "Be concise, operational, and avoid guessing."
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


async def run(
    db: AsyncSession,
    current_user: User,
    conversation_messages: list[dict],
) -> str:
    """Process the conversation using Claude tool calls when configured."""

    if not settings.is_anthropic_configured():
        return await _fallback_reply(db, current_user, conversation_messages)

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
            system=await _system_prompt(db, current_user),
            tools=TOOLS,
            messages=messages,
        )

        for _ in range(4):
            tool_uses = [block for block in response.content if block.type == "tool_use"]
            if not tool_uses:
                text = "".join(
                    block.text for block in response.content if block.type == "text"
                ).strip()
                return text or await _fallback_reply(db, current_user, conversation_messages)

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
                system=await _system_prompt(db, current_user),
                tools=TOOLS,
                messages=messages,
            )

        return "I could not complete the tool workflow in time. Try narrowing the question."
    except Exception:
        return await _fallback_reply(db, current_user, conversation_messages)


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

    client = AsyncAnthropic(api_key=settings.get_anthropic_api_key())
    messages = [
        {"role": msg["role"], "content": msg["content"]}
        for msg in conversation_messages
        if msg.get("role") in {"user", "assistant"} and msg.get("content")
    ]
    system_prompt = await _system_prompt(db, current_user)

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
                        yield text_chunk
                final_message = await stream.get_final_message()

            tool_uses = [b for b in final_message.content if b.type == "tool_use"]
            if not tool_uses:
                return

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
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": json.dumps(_json_ready(result), ensure_ascii=False),
                    }
                )
            messages.append({"role": "user", "content": tool_results})

        yield "\n\n[Reached tool-loop limit — narrow the question.]"
    except Exception as exc:
        logger.warning("[agent_service] stream failed, falling back: %s", exc)
        yield await _fallback_reply(db, current_user, conversation_messages)
