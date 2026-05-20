"""Conversational agent service with live-data tools."""

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
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
_MEM_ENTITY_RE = re.compile(r"\b(?:[A-Z]{2,}-?\d{2,}|[1-9]\d{1,5})\b")
_MEM_TOPIC_KEYWORDS = (
    "recommendation", "critical", "high risk", "churn", "overview",
    "draft", "similar", "metrics", "trend", "anomaly", "summary",
    "pipeline", "compare", "history", "outcome", "default",
    "fraud", "readmission",
)


@dataclass
class ChatResult:
    """Result from a single chat turn, carrying reply text + tool context."""
    reply: str
    tool_context: dict[str, Any] = field(default_factory=dict)
    tools_called: list[str] = field(default_factory=list)


from app.config.settings import settings
from app.infrastructure.database.client_queries import (
    ClientDBError,
    compute_risk,
    fetch_entities,
    fetch_entity_by_id,
    fetch_entity_trend,
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

from sqlalchemy import func as sa_func, select as sa_select
from app.infrastructure.database.models.entity_profile import EntityProfile

from app.agents.prompts.conversational import (
    CLARIFICATION_REPLY_PROMPT,
    DATA_ACCESS_REPLY_PROMPT,
    GREETING_REPLY_PROMPT,
    HELP_REPLY_PROMPT,
    OFF_TOPIC_REPLY_PROMPT,
    render_chat_system_prompt,
    reply_contains_em_dash,
    sanitize_pulse_reply,
)
from app.services.intent_router import (
    CONVERSATIONAL_INTENTS,
    apply_pipeline_intent_override,
    build_fastpath_args,
    classify_intent,
    filter_tools_by_intent,
    resolve_followup_query,
)

CHAT_LOGGER = logging.getLogger("pulse.chat")


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
    "data_access": DATA_ACCESS_REPLY_PROMPT,
    "off_topic": OFF_TOPIC_REPLY_PROMPT,
    "unknown": CLARIFICATION_REPLY_PROMPT,
}

_DATA_ACCESS_PATTERNS = (
    "database", "data base", "sql", "schema", "table", "tables", "raw data",
    "query my db", "query the db",
)


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
    max_tokens = 450 if intent_name in ("help", "data_access") else 320
    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=settings.GROQ_LLM_MODEL_FAST,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(state, default=str)[:2000]},
                ],
                temperature=0.78,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            ),
            timeout=8.0,
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
        "name": "get_pipeline_status",
        "description": "Get autonomous pipeline run status: active run, last completed run, entity counts, recommendations generated.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_pipeline_detail",
        "description": (
            "Get detailed breakdown of the latest pipeline run: per-step timing and success, "
            "ML model metrics (accuracy, F1, AUC), top feature importances, and RAG statistics. "
            "Use when the user asks about pipeline steps, model performance, or how long something took."
        ),
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
        "name": "get_outcome_analysis",
        "description": (
            "Get outcome/target analysis: how many entities hit the target condition "
            "(e.g. churned, defaulted, readmitted, delayed), outcome rate, and breakdown "
            "by risk tier. Use when the user asks about outcomes, target rates, or event counts."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_entity_trend",
        "description": (
            "Get historical signal values over time for one entity. Shows how key metrics "
            "evolved across records. Use when the user asks about trends, history, or "
            "changes over time for a specific entity."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "Entity ID to get trend for"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "Max data points"},
            },
            "required": ["entity_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "compare_pipeline_runs",
        "description": (
            "Compare the two most recent pipeline runs: entity count changes, risk tier shifts, "
            "recommendation count deltas, and performance differences. Use when the user asks "
            "'what changed', 'compare runs', or 'any differences since last time'."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
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


def _condense_tool_result(result: Any, max_chars: int = 500) -> Any:
    """Condense a tool result dict for persistence as turn metadata.

    Keeps the structure but truncates long strings and limits list sizes
    so the persisted context is compact enough to inject into future prompts.
    Never produces invalid JSON — progressively drops keys if still too large.
    """
    if isinstance(result, dict):
        condensed = {}
        for k, v in result.items():
            if isinstance(v, str) and len(v) > 200:
                condensed[k] = v[:200] + "..."
            elif isinstance(v, list) and len(v) > 5:
                condensed[k] = v[:5]
                condensed[f"{k}_total"] = len(v)
            elif isinstance(v, dict):
                # Nested dicts: convert to string if too large.
                nested = json.dumps(v, default=str)
                if len(nested) > 200:
                    condensed[k] = nested[:200] + "..."
                else:
                    condensed[k] = v
            else:
                condensed[k] = v
        # If still too large, progressively drop the heaviest keys.
        raw = json.dumps(condensed, default=str)
        while len(raw) > max_chars and condensed:
            # Find the key whose serialized value is largest and drop it.
            heaviest = max(condensed, key=lambda k: len(json.dumps(condensed[k], default=str)))
            del condensed[heaviest]
            raw = json.dumps(condensed, default=str)
        return condensed
    if isinstance(result, str) and len(result) > max_chars:
        return result[:max_chars]
    return result


async def _overview(db: AsyncSession, org_id: UUID) -> dict:
    """Get overview with risk breakdown.

    Prefers ML-predicted profiles from entity_profiles (written by the
    pipeline's risk scoring step) when available. Falls back to live
    recomputation from the client DB when no profiles exist.
    """
    # Try ML-predicted profiles first.
    profile_count = await db.scalar(
        sa_select(sa_func.count())
        .select_from(EntityProfile)
        .where(EntityProfile.org_id == org_id, EntityProfile.is_latest.is_(True))
    ) or 0

    if profile_count > 0:
        # Use persisted ML-predicted risk tiers.
        profiles = list(
            (await db.execute(
                sa_select(EntityProfile)
                .where(EntityProfile.org_id == org_id, EntityProfile.is_latest.is_(True))
                .order_by(EntityProfile.risk_score.desc().nullslast())
            )).scalars().all()
        )
        # Map display tiers back to internal tier names for consistency.
        _tier_map = {"high": "high", "medium": "medium", "low": "low", "healthy": "low"}
        breakdown = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for p in profiles:
            tier = _tier_map.get((p.risk_tier or "low").lower(), "low")
            breakdown[tier] += 1
        top = profiles[:3]
        active_recs = await RecommendationRepository(db).list_by_org(org_id, status="open")
        return {
            "total_entities": len(profiles),
            "risk_breakdown": breakdown,
            "active_recommendations": len(active_recs),
            "top_at_risk": [
                {
                    "entity_id": p.entity_id,
                    "entity_label": p.entity_name,
                    "risk_score": float(p.risk_score or 0),
                    "risk_tier": _tier_map.get((p.risk_tier or "low").lower(), "low"),
                }
                for p in top
            ],
        }

    # Fallback: live recomputation from client DB.
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
    """Get filtered entity summaries.

    Prefers ML-predicted profiles from entity_profiles when available.
    """
    # Try ML-predicted profiles first.
    profile_count = await db.scalar(
        sa_select(sa_func.count())
        .select_from(EntityProfile)
        .where(EntityProfile.org_id == org_id, EntityProfile.is_latest.is_(True))
    ) or 0

    _tier_map = {"high": "high", "medium": "medium", "low": "low", "healthy": "low"}

    if profile_count > 0:
        conds = [
            EntityProfile.org_id == org_id,
            EntityProfile.is_latest.is_(True),
        ]
        if risk_tier:
            # Map internal tier to display tiers for the DB query.
            display_tiers = [k for k, v in _tier_map.items() if v == risk_tier]
            if not display_tiers:
                display_tiers = [risk_tier]
            conds.append(EntityProfile.risk_tier.in_([t.title() for t in display_tiers] + display_tiers))
        if search:
            like = f"%{search.lower()}%"
            conds.append(
                sa_func.lower(EntityProfile.entity_name).like(like)
                | sa_func.lower(EntityProfile.entity_id).like(like)
            )

        profiles = list(
            (await db.execute(
                sa_select(EntityProfile)
                .where(*conds)
                .order_by(EntityProfile.risk_score.desc().nullslast())
                .limit(limit)
            )).scalars().all()
        )
        total = await db.scalar(
            sa_select(sa_func.count()).select_from(EntityProfile).where(*conds)
        ) or 0

        rows = [
            {
                "entity_id": p.entity_id,
                "entity_label": p.entity_name,
                "risk_score": float(p.risk_score or 0),
                "risk_tier": _tier_map.get((p.risk_tier or "low").lower(), "low"),
                "signals": (p.profile_data or {}).get("signal_values", {}),
            }
            for p in profiles
        ]
        return {"entities": rows, "total": int(total)}

    # Fallback: live recomputation from client DB.
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


def _pipeline_run_snapshot(run) -> dict:
    return {
        "id": str(run.id),
        "status": run.status,
        "current_step": run.current_step,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "entities_scored": run.entities_scored,
        "critical_count": run.critical_count,
        "high_count": run.high_count,
        "recommendations_generated": run.recommendations_generated,
        "error": run.error,
    }


async def _pipeline_status(db: AsyncSession, org_id: UUID) -> dict:
    """Structured pipeline run status for tools and synthesis."""
    from app.infrastructure.database.repositories.pipeline_run_repository import (
        PipelineRunRepository,
    )

    repo = PipelineRunRepository(db)
    try:
        recent = await repo.list_by_org(org_id, limit=5)
    except Exception:
        return {"error": "Could not load pipeline runs", "has_completed_run": False}

    if not recent:
        return {
            "has_completed_run": False,
            "active_run": None,
            "last_completed_run": None,
            "message": "No pipeline run has completed yet for this organization.",
        }

    active = next((r for r in recent if r.status in ("queued", "running")), None)
    last_done = next((r for r in recent if r.status in ("succeeded", "failed")), None)
    return {
        "has_completed_run": last_done is not None and last_done.status == "succeeded",
        "active_run": _pipeline_run_snapshot(active) if active else None,
        "last_completed_run": _pipeline_run_snapshot(last_done) if last_done else None,
    }


async def _pipeline_detail(db: AsyncSession, org_id: UUID) -> dict:
    """Rich pipeline run detail: per-step timing, model metrics, feature importances."""
    from app.infrastructure.database.repositories.pipeline_run_repository import (
        PipelineRunRepository,
    )

    repo = PipelineRunRepository(db)
    try:
        recent = await repo.list_by_org(org_id, limit=3)
    except Exception:
        return {"error": "Could not load pipeline runs"}

    last_done = next((r for r in recent if r.status in ("succeeded", "failed")), None)
    if not last_done:
        return {"error": "No completed pipeline run found for this organization."}

    # Step-level breakdown.
    steps = []
    for s in (last_done.step_metrics or []):
        steps.append({
            "step": s.get("step"),
            "success": s.get("success"),
            "duration_ms": s.get("duration_ms"),
            "llm_calls": s.get("llm_calls", 0),
            "tool_calls": s.get("tool_calls", 0),
            "tokens": s.get("total_tokens", 0),
            "error": s.get("error"),
        })

    # Model metrics from the run artifact file.
    model_metrics = {}
    feature_importances = []
    try:
        from pathlib import Path
        import json as _json
        _RUN_LOG_DIR = Path("logs/pipeline_runs")
        if _RUN_LOG_DIR.exists():
            # Find the artifact JSON for this run.
            for artifact_file in sorted(_RUN_LOG_DIR.glob(f"*{last_done.id}*.json"), reverse=True):
                artifact = _json.loads(artifact_file.read_text(encoding="utf-8"))
                model_metrics = artifact.get("model_metrics") or {}
                # Feature importances are usually in risk_summary or model_metrics.
                risk_summary = artifact.get("risk_summary") or {}
                feature_importances = risk_summary.get("top_risk_drivers") or []
                if not feature_importances:
                    feature_importances = model_metrics.get("feature_importances") or []
                break
    except Exception as exc:
        logger.debug("[pipeline_detail] artifact read failed: %s", exc)

    rag_metrics = last_done.rag_metrics or {}

    return {
        "run_id": str(last_done.id),
        "status": last_done.status,
        "completed_at": last_done.completed_at.isoformat() if last_done.completed_at else None,
        "duration_ms": last_done.duration_ms,
        "entities_scored": last_done.entities_scored,
        "critical_count": last_done.critical_count,
        "high_count": last_done.high_count,
        "recommendations_generated": last_done.recommendations_generated,
        "total_llm_calls": last_done.total_llm_calls,
        "total_tool_calls": last_done.total_tool_calls,
        "total_tokens": last_done.total_tokens,
        "steps": steps,
        "model_metrics": model_metrics,
        "feature_importances": feature_importances[:10],
        "rag_metrics": {
            "eval": rag_metrics.get("eval"),
        } if rag_metrics else {},
    }


async def _outcome_analysis(db: AsyncSession, org_id: UUID) -> dict:
    """Outcome/target analysis from the client DB's target column or entity profiles.

    Works for any domain: churn, fraud, readmission, delivery failure, etc.
    The target column is configured per-org in the schema mapping.
    """
    # Try entity profiles first (ML-predicted).
    total_profiles = await db.scalar(
        sa_select(sa_func.count())
        .select_from(EntityProfile)
        .where(EntityProfile.org_id == org_id, EntityProfile.is_latest.is_(True))
    ) or 0

    if total_profiles > 0:
        # Count by risk tier.
        _tier_map = {"high": "high", "medium": "medium", "low": "low", "healthy": "low"}
        profiles = list(
            (await db.execute(
                sa_select(EntityProfile.risk_tier, sa_func.count().label("cnt"))
                .where(EntityProfile.org_id == org_id, EntityProfile.is_latest.is_(True))
                .group_by(EntityProfile.risk_tier)
            )).all()
        )
        tier_counts = {}
        for tier_val, cnt in profiles:
            mapped = _tier_map.get((tier_val or "low").lower(), "low")
            tier_counts[mapped] = tier_counts.get(mapped, 0) + cnt

        # Also try to get the actual outcome count from client DB.
        outcome_data = await _query_outcome_from_client_db(db, org_id)

        result = {
            "total_entities": total_profiles,
            "risk_tier_breakdown": tier_counts,
            "high_risk_count": tier_counts.get("high", 0),
        }
        if outcome_data:
            result.update(outcome_data)
        return result

    # Direct client DB query.
    outcome_data = await _query_outcome_from_client_db(db, org_id)
    if outcome_data:
        return outcome_data
    return {"error": "No entity profiles or outcome data available. Run a pipeline first."}


async def _query_outcome_from_client_db(db: AsyncSession, org_id: UUID) -> dict | None:
    """Query the target/outcome column from the client DB."""
    try:
        mapping = await get_schema_mapping(db, org_id)
        target_col = getattr(mapping, "target_column", None)
        if not target_col:
            return None

        entities = await fetch_entities(db, org_id, mapping)
        if not entities:
            return None

        # Count positive-outcome entities.
        total = len(entities)
        positive = sum(
            1 for e in entities
            if e.get(target_col) in (1, True, "1", "true", "yes", "True", "Yes")
        )
        return {
            "total_entities": total,
            "positive_outcome_count": positive,
            "outcome_rate": round(positive / total * 100, 2) if total > 0 else 0,
            "negative_outcome_count": total - positive,
            "target_column": target_col,
        }
    except Exception as exc:
        logger.debug("[outcome_analysis] client DB query failed: %s", exc)
        return None


async def _entity_trend(
    db: AsyncSession, org_id: UUID, entity_id: str, limit: int = 100,
) -> dict:
    """Get historical signal trend for one entity."""
    try:
        mapping = await get_schema_mapping(db, org_id)
        points = await fetch_entity_trend(db, org_id, entity_id, mapping, limit=limit)
        return {
            "entity_id": entity_id,
            "data_points": len(points),
            "trend": points,
        }
    except ClientDBError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"Could not fetch trend: {exc}"}


async def _compare_runs(db: AsyncSession, org_id: UUID) -> dict:
    """Compare the two most recent completed pipeline runs."""
    from app.infrastructure.database.repositories.pipeline_run_repository import (
        PipelineRunRepository,
    )

    repo = PipelineRunRepository(db)
    try:
        recent = await repo.list_by_org(org_id, limit=10)
    except Exception:
        return {"error": "Could not load pipeline runs"}

    completed = [r for r in recent if r.status in ("succeeded", "failed")]
    if len(completed) < 2:
        return {"error": "Need at least 2 completed runs to compare. Only found " + str(len(completed)) + "."}

    current, previous = completed[0], completed[1]

    def _snap(run):
        return {
            "run_id": str(run.id),
            "status": run.status,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "entities_scored": run.entities_scored,
            "critical_count": run.critical_count,
            "high_count": run.high_count,
            "recommendations_generated": run.recommendations_generated,
            "duration_ms": run.duration_ms,
            "total_tokens": run.total_tokens,
        }

    return {
        "current_run": _snap(current),
        "previous_run": _snap(previous),
        "deltas": {
            "entities_scored": current.entities_scored - previous.entities_scored,
            "critical_count": current.critical_count - previous.critical_count,
            "high_count": current.high_count - previous.high_count,
            "recommendations_generated": current.recommendations_generated - previous.recommendations_generated,
            "duration_ms": (current.duration_ms or 0) - (previous.duration_ms or 0),
        },
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


def _extract_json_array(raw_text: str) -> list[dict]:
    """Parse a JSON array from LLM output, stripping markdown fences if present."""
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
    from app.api.dependencies.plan_gate import check_studio_dashboard_limit
    from app.services.studio_query_service import _inject_limit, _is_select_only
    from app.agents.tools.client_db import schema_columns_sql

    max_charts = max(1, min(max_charts, 6))

    try:
        await check_studio_dashboard_limit(db, org_id)
    except Exception as exc:
        from app.api.errors import PulseHTTPException
        if isinstance(exc, PulseHTTPException):
            detail = exc.detail
            msg = detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail)
            return {"error": msg}
        return {"error": str(exc)}

    # Step 1: Introspect schema
    schema_context = ""
    try:
        engine, conn = await open_client_engine(db, org_id)
        try:
            async with safe_client_connection(engine, conn) as client_conn:
                from sqlalchemy import text as _text
                db_type = getattr(conn, "db_type", None)
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
                        col_rows = (await client_conn.execute(_text(cols_sql), {"tname": tname})).all()
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

    # Step 2: LLM planning
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
        plan = _extract_json_array(raw_text)
    except Exception as exc:
        logger.warning("[build_dashboard] LLM planning failed: %s", exc)
        return {"error": "Agent could not generate a dashboard plan from the goal"}

    safe_plan = [
        spec for spec in plan
        if isinstance(spec, dict) and spec.get("sql") and _is_select_only(str(spec["sql"]))
    ]
    if not safe_plan:
        return {"error": "Generated SQL was not safe to execute — only SELECT statements are allowed"}

    # Step 3: Persist
    query_repo = StudioQueryRepository(db)
    viz_repo = StudioVisualizationRepository(db)
    dash_repo = StudioDashboardRepository(db)
    item_repo = StudioDashboardItemRepository(db)

    created_vizs = []
    for spec in safe_plan:
        safe_sql = _inject_limit(str(spec["sql"]).strip(), 5000)
        q = await query_repo.create(
            org_id, current_user.id,
            name=str(spec.get("query_name", "Query")),
            description=str(spec.get("description", "")),
            sql_text=safe_sql,
            connection_id=None,
        )
        viz = await viz_repo.create(
            org_id, q.id, current_user.id,
            name=str(spec.get("query_name", "Chart")),
            chart_type=str(spec.get("chart_type", "table")),
            config={k: v for k, v in (spec.get("config") or {}).items() if v is not None},
        )
        created_vizs.append(viz)

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
        org_id, current_user.id,
        name=dashboard_name_final,
        description=f"Auto-generated from goal: {goal[:200]}",
        is_public=is_public,
        slug=slug,
        layout=[],
    )

    layout = []
    for i, viz in enumerate(created_vizs):
        item = await item_repo.create(org_id, dashboard.id, viz.id, i)
        layout.append({"item_id": str(item.id), "x": (i % 2) * 6, "y": (i // 2) * 4, "w": 6, "h": 4})

    await dash_repo.update(dashboard, layout=layout)
    await db.commit()

    # Step 4: Audit + return
    try:
        await log_audit(
            db, org_id=org_id, user_id=current_user.id,
            action="studio.agent_build_dashboard",
            metadata={"goal": goal[:200], "charts": len(created_vizs)},
        )
    except Exception:
        pass

    return {
        "dashboard_id": str(dashboard.id),
        "dashboard_name": dashboard_name_final,
        "chart_count": len(created_vizs),
        "is_public": is_public,
        "slug": slug,
        "message": f"Dashboard '{dashboard_name_final}' created with {len(created_vizs)} chart(s).",
    }


async def _run_tool(
    name: str,
    tool_input: dict,
    db: AsyncSession,
    org_id: UUID,
    current_user: User | None = None,
) -> dict:
    try:
        result = None
        if name == "get_overview":
            result = await _overview(db, org_id)
            logger.info(
                "[_run_tool] get_overview: total=%s breakdown=%s",
                result.get("total_entities"), result.get("risk_breakdown"),
            )
            return result
        if name == "get_pipeline_status":
            return await _pipeline_status(db, org_id)
        if name == "get_pipeline_detail":
            return await _pipeline_detail(db, org_id)
        if name == "get_outcome_analysis":
            return await _outcome_analysis(db, org_id)
        if name == "get_entity_trend":
            return await _entity_trend(
                db, org_id,
                str(tool_input["entity_id"]),
                limit=int(tool_input.get("limit") or 100),
            )
        if name == "compare_pipeline_runs":
            return await _compare_runs(db, org_id)
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


def _build_context_window(
    conversation_messages: list[dict],
    window_size: int | None = None,
) -> tuple[list[dict], list[dict]]:
    """Split messages into (overflow, active_window).

    The active window is the most recent `window_size` messages; overflow is
    everything before that. Only user/assistant text messages count toward
    the window; tool_result blocks are always kept with their adjacent
    assistant message.

    Returns (overflow_messages, active_window_messages) where both are
    sublists of the original conversation_messages.
    """
    ws = window_size or settings.CHAT_CONTEXT_WINDOW_MESSAGES
    if len(conversation_messages) <= ws:
        return [], conversation_messages

    # Find the split point: we want at least `ws` messages in the active window,
    # but we don't want to cut in the middle of a tool-use exchange.
    split = len(conversation_messages) - ws
    # Walk forward from the split to find a clean user-message boundary.
    while split > 0 and split < len(conversation_messages):
        msg = conversation_messages[split]
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            break
        split += 1

    overflow = conversation_messages[:split]
    active = conversation_messages[split:]
    return overflow, active


async def _summarize_overflow(overflow_messages: list[dict]) -> str:
    """Compress overflow messages into a short context summary.

    Uses Groq-fast for speed. Falls back to a simple truncation when
    Groq is unavailable. Returns a 2-4 sentence summary.
    """
    if not overflow_messages:
        return ""

    # Build a compact transcript of the overflow.
    lines: list[str] = []
    for msg in overflow_messages:
        role = msg.get("role")
        content = msg.get("content")
        if not role or not content:
            continue
        if isinstance(content, str):
            lines.append(f"{role}: {content[:300]}")
        elif isinstance(content, list):
            # tool_result blocks — just note the tool names.
            tool_names = [
                c.get("name", "tool") for c in content
                if isinstance(c, dict) and c.get("type") == "tool_use"
            ]
            if tool_names:
                lines.append(f"{role}: [called tools: {', '.join(tool_names)}]")
    transcript = "\n".join(lines[-20:])  # cap to prevent huge prompts

    if not transcript.strip():
        return ""

    # Try Groq-fast summarization.
    try:
        from groq import AsyncGroq
        if settings.is_groq_configured():
            client = AsyncGroq(api_key=settings.groq_api_key, max_retries=1, timeout=6.0)
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=settings.GROQ_LLM_MODEL_FAST,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Summarize this conversation excerpt in 2-4 sentences, third person. "
                                "Capture: what the user asked about, which entities/IDs were discussed, "
                                "what data was surfaced, and any decisions made. Be specific about "
                                "entity IDs and numbers. No JSON, just plain text."
                            ),
                        },
                        {"role": "user", "content": transcript},
                    ],
                    temperature=0.0,
                    max_tokens=200,
                ),
                timeout=6.0,
            )
            summary = (response.choices[0].message.content or "").strip()
            if summary:
                return summary
    except Exception as exc:
        logger.debug("[context_window] Groq summarization failed: %s", exc)

    # Fallback: extract key lines.
    user_lines = [l for l in lines if l.startswith("user:")]
    if user_lines:
        return "Earlier in this conversation, the user asked: " + "; ".join(
            l.replace("user: ", "") for l in user_lines[-3:]
        )
    return ""


async def _system_prompt(
    db: AsyncSession,
    current_user: User,
    *,
    recalled_block: str = "",
    handoff_block: str = "",
    overflow_summary: str = "",
) -> str:
    org = await OrganizationRepository(db).get_by_id(current_user.org_id)
    org_name = org.name if org else "this organization"
    entity_label = org.entity_label if org and org.entity_label else "entities"
    goal_label = org.goal_label if org and org.goal_label else "improve operations"
    industry = (org.industry if org and org.industry else "") or ""
    context = org.business_context if org and org.business_context else "No business context configured."

    pipeline_block = await _pipeline_context_block(db, current_user.org_id)
    memory = await _load_user_memory(db, current_user)
    memory_block = _format_memory_for_prompt(memory)

    # Inject overflow summary if present (context window management).
    overflow_block = ""
    if overflow_summary:
        overflow_block = (
            "## Conversation history (summarized older messages)\n"
            f"{overflow_summary}\n"
            "The messages below are the most recent; use the summary above "
            "for context on what was discussed earlier.\n\n"
        )

    return render_chat_system_prompt(
        org_name=org_name,
        entity_label=entity_label,
        goal_label=goal_label,
        business_context=context,
        industry=industry,
        pipeline_block=pipeline_block,
        memory_block=memory_block,
        handoff_block=handoff_block + overflow_block,
        recalled_block=recalled_block,
    )


async def _org_context(db: AsyncSession, current_user: User) -> tuple[str, str]:
    """Return (industry, business_context) for grounding checks."""
    try:
        org = await OrganizationRepository(db).get_by_id(current_user.org_id)
    except Exception:
        return "", ""
    if not org:
        return "", ""
    return (
        (org.industry or "") or "",
        (org.business_context or "") or "",
    )


async def _pipeline_context_block(db: AsyncSession, org_id: UUID) -> str:
    """Compose a short autonomous-pipeline status section for the system prompt."""
    try:
        status = await _pipeline_status(db, org_id)
    except Exception:
        return ""

    if status.get("message"):
        return (
            "Autonomous pipeline status: no pipeline run has completed yet for "
            "this organization. If asked about the latest analysis, say so and "
            "fall back to live tool calls.\n"
        )

    lines = ["Autonomous pipeline status:"]
    active = status.get("active_run")
    last_done = status.get("last_completed_run")
    if active:
        lines.append(
            f"- A pipeline run is currently {active['status']} "
            f"(step '{active.get('current_step') or 'unknown'}', id={active['id']})."
        )
    if last_done:
        ts = last_done.get("completed_at") or "unknown time"
        if last_done.get("status") == "succeeded":
            lines.append(
                f"- Last successful run completed at {ts}: "
                f"{last_done.get('entities_scored')} entities scored "
                f"({last_done.get('critical_count')} critical, {last_done.get('high_count')} high), "
                f"{last_done.get('recommendations_generated')} recommendations generated."
            )
        else:
            lines.append(
                f"- Last run failed at {ts}: {last_done.get('error') or 'unknown error'}."
            )
    if not active and not last_done:
        lines.append("- No completed runs available.")

    lines.append(
        "Treat these numbers as the latest persisted snapshot; if the user asks "
        "for live numbers, use the tools to re-query the client database."
    )
    return "\n".join(lines) + "\n"


def _uses_local_currency(industry: str, business_context: str) -> bool:
    combined = f"{industry} {business_context}".lower()
    return any(k in combined for k in ("bank", "financial", "nigeria", "naira", "union bank"))


def _reply_has_forbidden_currency(reply: str, industry: str, business_context: str) -> bool:
    if "$" not in reply:
        return False
    return _uses_local_currency(industry, business_context)


def _format_recommendations_fallback(data: dict) -> str:
    recs = (data or {}).get("recommendations") or []
    if not recs:
        return "You're clear on open recommendations right now; nothing queued."
    parts = [f"You've got {len(recs)} open recommendation{'s' if len(recs) != 1 else ''}."]
    for rec in recs[:8]:
        eid = rec.get("entity_id", "?")
        title = rec.get("title") or "Follow-up needed"
        action = rec.get("suggested_action") or rec.get("reasoning") or ""
        snippet = f"{title}. {action}".strip() if action else title
        parts.append(f"For customer {eid}: {snippet}")
    if len(recs) > 8:
        parts.append(f"…and {len(recs) - 8} more. Ask if you want the rest.")
    parts.append("Tell me which customer to zoom in on, or say the word and I'll prioritize by urgency.")
    return " ".join(parts)


_CHAT_LOG_REPLY_MAX = 4000


def _chat_log_content(text: str | None) -> str:
    """Single-line log field; newlines would break tail -f."""
    if not text:
        return "-"
    return " ".join(text.split())


def _log_chat_turn(
    *,
    conversation_id: UUID | None,
    intent: str | None,
    confidence: float | None,
    path: str,
    tools_called: list[str] | None = None,
    latency_ms: int | None = None,
    user_message: str | None = None,
    assistant_reply: str | None = None,
    extra: str | None = None,
) -> None:
    cid = conversation_id or "-"
    meta = (
        f"[Chat] conversation_id={cid} intent={intent or '-'} confidence="
        f"{f'{confidence:.2f}' if confidence is not None else '-'} "
        f"path={path} tools={','.join(tools_called) if tools_called else '-'} "
        f"latency_ms={latency_ms if latency_ms is not None else '-'} "
        f"{extra or ''}"
    ).strip()
    user_line = f"{meta} role=user content={_chat_log_content(user_message)}"
    logger.info(user_line)
    CHAT_LOGGER.info(user_line)
    if assistant_reply is not None:
        body = _chat_log_content(assistant_reply)
        if len(assistant_reply) > _CHAT_LOG_REPLY_MAX:
            body = f"{body[:_CHAT_LOG_REPLY_MAX]}... ({len(assistant_reply)} chars total)"
        assistant_line = f"[Chat] conversation_id={cid} path={path} role=assistant content={body}"
        logger.info(assistant_line)
        CHAT_LOGGER.info(assistant_line)


async def _guarded_synthesis(
    db: AsyncSession,
    current_user: User,
    conversation_messages: list[dict],
    retrieved_data: dict,
    *,
    base_system_prompt: str,
    industry: str,
    business_context: str,
) -> str:
    """Synthesis with retries for em dashes, forbidden $, then deterministic fallback."""
    extra_parts: list[str] = []
    reply = await synthesis_agent_run(
        db, current_user, conversation_messages, retrieved_data,
        base_system_prompt=base_system_prompt,
    )
    if reply and reply_contains_em_dash(reply):
        extra_parts.append(
            "Do not use em dashes (—) or en dashes (–). Use commas, colons, or periods only."
        )
    if reply and _reply_has_forbidden_currency(reply, industry, business_context):
        extra_parts.append(
            "Remove all dollar ($) amounts. Use only text from the tool data. "
            "Use local currency wording only if amounts appear in the data."
        )
    if extra_parts:
        reply = await synthesis_agent_run(
            db, current_user, conversation_messages, retrieved_data,
            base_system_prompt=base_system_prompt,
            extra_instruction=" ".join(extra_parts),
        )
    if reply and _reply_has_forbidden_currency(reply, industry, business_context):
        rec_data = retrieved_data.get("get_recommendations")
        if rec_data:
            return sanitize_pulse_reply(_format_recommendations_fallback(rec_data))
    return sanitize_pulse_reply(reply or "")


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


def _is_data_access_question(message: str) -> bool:
    lower = message.lower()
    return any(p in lower for p in _DATA_ACCESS_PATTERNS)


def _recent_turns_for_prompt(conversation_messages: list[dict], limit: int = 4) -> list[dict]:
    turns = []
    for msg in conversation_messages[-limit:]:
        role = msg.get("role")
        content = msg.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            turns.append({"role": role, "content": content[:400]})
    return turns


async def _conversational_reply(
    db: AsyncSession,
    current_user: User,
    intent_name: str,
    user_message: str,
    *,
    conversation_messages: list[dict] | None = None,
) -> str:
    """Tool-free reply for greeting / help / off_topic / unknown.

    Pulls org context (name, entity_label, goal_label) so the reply is grounded
    in the org's vocabulary instead of generic Pulse boilerplate.
    """
    if intent_name == "help" and _is_data_access_question(user_message):
        intent_name = "data_access"
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
        "recent_turns": _recent_turns_for_prompt(conversation_messages or []),
    }

    # Try LLM-crafted reply first (warmer + tone-matched).
    crafted = await _craft_conversational_reply(intent_name, state)
    if crafted:
        return sanitize_pulse_reply(crafted)

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
        opener = f"Hey {user_first}," if user_first else "Hey,"
        return sanitize_pulse_reply(
            f"{opener} I'm Pulse AI, your intelligent copilot for {org_name}. "
            f"I can help with {entity_label}, risk, and recommendations as you "
            f"{goal_label}. Try \"what's our status?\" or "
            f"\"what was my latest pipeline run about?\" to get started."
        )

    if intent_name == "data_access":
        return sanitize_pulse_reply(
            f"I'm Pulse AI, your copilot. I can't run arbitrary SQL or browse your schema, but I can answer "
            f"real questions about {org_name}'s live {entity_label}: risk, outcomes, trends, "
            f"recommendations, and more. Try \"what's our status?\" or "
            f"\"what recommendations can you give me?\""
        )

    if intent_name == "help":
        return sanitize_pulse_reply(
            f"I'm Pulse AI, your intelligent copilot for {org_name}. I can give you the big picture "
            f"on {entity_label}, pull up a specific one by ID, surface what to action today, "
            f"check the latest pipeline run, dig into model performance, track trends, "
            f"find lookalikes, or draft outreach. "
            f"Try \"show critical {entity_label}\" or \"tell me about 628\". "
            f"What do you want to look at first?"
        )

    if intent_name == "off_topic":
        return sanitize_pulse_reply(
            f"That's a bit outside what I cover as Pulse AI. I'm here for {org_name}'s "
            f"{entity_label} and what to do about them. Try \"what's our status?\" or "
            f"\"show critical {entity_label}\" and I'll jump in."
        )

    return sanitize_pulse_reply(
        f"I want to make sure I help with the right thing. Do you mean the overall picture, "
        f"a specific {singular}, or what's on your action list? "
        f"\"What's our status?\" and \"what should I action today?\" are easy starters."
    )


async def run(
    db: AsyncSession,
    current_user: User,
    conversation_messages: list[dict],
    *,
    conversation_id: UUID | None = None,
) -> ChatResult:
    """Process the conversation using Claude tool calls when configured.

    Returns a ChatResult containing the reply text and any tool context
    from the turn, so the caller can persist tool context for follow-ups.
    """

    t0 = time.perf_counter()
    chat_path = "react"
    chat_intent: str | None = None
    chat_confidence: float | None = None
    chat_tools: list[str] = []

    def _finish(
        reply: str,
        path: str,
        intent: str | None = None,
        conf: float | None = None,
        *,
        effective_query: str | None = None,
    ) -> str:
        clean = sanitize_pulse_reply(reply)
        _log_chat_turn(
            conversation_id=conversation_id,
            intent=intent or chat_intent,
            confidence=conf if conf is not None else chat_confidence,
            path=path,
            tools_called=chat_tools or None,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            user_message=latest_user,
            assistant_reply=clean,
            extra=f"effective_query={effective_query}" if effective_query else None,
        )
        return ChatResult(
            reply=clean,
            tool_context=dict(last_tool_context),
            tools_called=list(chat_tools),
        )

    latest_user = _latest_user_message(conversation_messages)
    effective_user = latest_user
    last_tool_context: dict[str, Any] = {}
    followup_rewrite = resolve_followup_query(conversation_messages, latest_user)
    if followup_rewrite:
        effective_user = followup_rewrite
        CHAT_LOGGER.info(
            "[Chat] follow-up rewrite conversation_id=%s from=%r to=%r",
            conversation_id or "-", latest_user, followup_rewrite,
        )

    if not settings.is_anthropic_configured():
        return _finish(
            await _fallback_reply(db, current_user, conversation_messages),
            "fallback",
        )

    industry, business_context = await _org_context(db, current_user)
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

    # Context window management: prevent LLM context overflow on long conversations.
    overflow_msgs, windowed_messages = _build_context_window(conversation_messages)
    overflow_summary = ""
    if overflow_msgs and len(overflow_msgs) >= settings.CHAT_CONTEXT_SUMMARY_OVERFLOW:
        overflow_summary = await _summarize_overflow(overflow_msgs)
        logger.info(
            "[agent_service] context window: %d total, %d overflow → %d active, summary=%d chars",
            len(conversation_messages), len(overflow_msgs),
            len(windowed_messages), len(overflow_summary),
        )
    elif overflow_msgs:
        # Small overflow: just note it was truncated.
        overflow_summary = (
            f"(Earlier {len(overflow_msgs)} messages were exchanged but are not shown. "
            "The conversation continues below.)"
        )

    # Semantic intent detection: fast-path high-confidence simple intents past
    # the full ReAct loop; otherwise prefilter the tool list for the ReAct loop.
    tools_for_run = TOOLS
    if settings.CHAT_INTENT_DETECTION_ENABLED and latest_user:
        recent_for_intent = []
        for m in conversation_messages[-6:]:
            if m.get("role") not in {"user", "assistant"} or not isinstance(m.get("content"), str):
                continue
            entry = {"role": m["role"], "content": m["content"]}
            # Inject tool hints so the classifier knows what data context is active.
            if m.get("role") == "assistant" and m.get("tools_called"):
                tools_hint = ", ".join(m["tools_called"])
                entry["content"] += f" [tools used: {tools_hint}]"
            recent_for_intent.append(entry)
        intent = await classify_intent(effective_user, convo_history=recent_for_intent)
        if intent:
            intent = apply_pipeline_intent_override(intent, effective_user)
            chat_intent = intent.intent
            chat_confidence = intent.confidence

        # Conversational intents — greeting / help / off_topic / (low-confidence) unknown.
        # Skip ReAct and tool calls entirely; return a context-aware reply.
        if (
            not followup_rewrite
            and intent
            and intent.intent in CONVERSATIONAL_INTENTS
            and intent.confidence >= 0.5
        ):
            logger.info(
                "[agent_service] conversational intent=%s confidence=%.2f",
                intent.intent, intent.confidence,
            )
            reply = await _conversational_reply(
                db, current_user, intent.intent, latest_user,
                conversation_messages=conversation_messages,
            )
            return _finish(
                reply, "conversational", intent.intent, intent.confidence,
                effective_query=followup_rewrite,
            )

        if intent and intent.confidence >= settings.CHAT_INTENT_FASTPATH_CONFIDENCE:
            fp = build_fastpath_args(intent, effective_user)
            if fp is not None:
                tool_name, tool_args = fp
                chat_tools = [tool_name]
                chat_path = "fastpath"
                logger.info(
                    "[agent_service] intent fast-path: intent=%s confidence=%.2f tool=%s",
                    intent.intent, intent.confidence, tool_name,
                )
                try:
                    tool_result = await _run_tool(
                        tool_name, tool_args, db, current_user.org_id,
                        current_user=current_user,
                    )
                    last_tool_context[tool_name] = _condense_tool_result(
                        _json_ready(tool_result)
                    )
                    base_system = await _system_prompt(
                        db, current_user,
                        recalled_block=recalled_block, handoff_block=handoff_block,
                        overflow_summary=overflow_summary,
                    )
                    synthesis_reply = await _guarded_synthesis(
                        db, current_user, windowed_messages,
                        {tool_name: _json_ready(tool_result)},
                        base_system_prompt=base_system,
                        industry=industry,
                        business_context=business_context,
                    )
                    if synthesis_reply:
                        try:
                            await reflect_and_commit(current_user, latest_user, synthesis_reply)
                        except Exception as exc:
                            logger.debug("[agent_service] reflect_and_commit failed: %s", exc)
                        return _finish(
                            synthesis_reply, "fastpath", intent.intent, intent.confidence,
                            effective_query=followup_rewrite,
                        )
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
        chat_path = "split"
        base_system = await _system_prompt(
            db, current_user, recalled_block=recalled_block, handoff_block=handoff_block,
            overflow_summary=overflow_summary,
        )
        try:
            reply_text = await run_split(
                db, current_user, windowed_messages,
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
            return _finish(reply_text, "split", chat_intent, chat_confidence)
        # If split returned empty, fall through to single-agent path below.

    reply_text: str = ""
    chat_path = "react"
    try:
        client = AsyncAnthropic(api_key=settings.get_anthropic_api_key())
        messages = [
            {"role": msg["role"], "content": msg["content"]}
            for msg in windowed_messages
            if msg.get("role") in {"user", "assistant"} and msg.get("content")
        ]

        response = await client.messages.create(
            model=settings.ANTHROPIC_LLM_MODEL,
            max_tokens=900,
            system=await _system_prompt(db, current_user, recalled_block=recalled_block, handoff_block=handoff_block, overflow_summary=overflow_summary),
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
                chat_tools.append(tool_use.name)
                result = await _run_tool(
                    tool_use.name,
                    dict(tool_use.input or {}),
                    db,
                    current_user.org_id,
                    current_user=current_user,
                )
                last_tool_context[tool_use.name] = _condense_tool_result(
                    _json_ready(result)
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
                system=await _system_prompt(db, current_user, recalled_block=recalled_block, handoff_block=handoff_block, overflow_summary=overflow_summary),
                tools=tools_for_run,
                messages=messages,
            )
        else:
            reply_text = "I could not complete the tool workflow in time. Try narrowing the question."
    except Exception as exc:
        chat_path = "fallback"
        CHAT_LOGGER.exception(
            "[Chat] run failed conversation_id=%s user=%r: %s",
            conversation_id or "-", latest_user, exc,
        )
        logger.exception("[agent_service] run failed: %s", exc)
        try:
            reply_text = await _fallback_reply(db, current_user, conversation_messages)
        except Exception as fb_exc:
            CHAT_LOGGER.exception("[Chat] fallback also failed: %s", fb_exc)
            reply_text = (
                "I hit an error loading live data for that question. "
                "Try \"what's our status?\" or \"show critical customers\" again."
            )

    # Memory commit is best-effort; never let it block or fail the reply.
    if latest_user and reply_text:
        try:
            await reflect_and_commit(current_user, latest_user, reply_text)
        except Exception as exc:
            logger.debug("[agent_service] reflect_and_commit failed: %s", exc)

    return _finish(
        reply_text, chat_path, chat_intent, chat_confidence,
        effective_query=followup_rewrite,
    )


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

    # Context window management (same as run()).
    overflow_msgs, windowed_messages = _build_context_window(conversation_messages)
    overflow_summary = ""
    if overflow_msgs and len(overflow_msgs) >= settings.CHAT_CONTEXT_SUMMARY_OVERFLOW:
        overflow_summary = await _summarize_overflow(overflow_msgs)
    elif overflow_msgs:
        overflow_summary = (
            f"(Earlier {len(overflow_msgs)} messages were exchanged but are not shown. "
            "The conversation continues below.)"
        )

    client = AsyncAnthropic(api_key=settings.get_anthropic_api_key())
    messages = [
        {"role": msg["role"], "content": msg["content"]}
        for msg in windowed_messages
        if msg.get("role") in {"user", "assistant"} and msg.get("content")
    ]
    system_prompt = await _system_prompt(db, current_user, recalled_block=recalled_block, handoff_block=handoff_block, overflow_summary=overflow_summary)

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
