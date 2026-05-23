"""Semantic intent classifier for the conversational agent.

A fast Groq call ahead of the ReAct loop classifies the user's message into one
of a small intent set, extracts entity IDs and filters, and returns a confidence"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from groq import AsyncGroq

from app.agents.prompts.intent_classifier import INTENT_CLASSIFIER_PROMPT
from app.config.settings import settings

logger = logging.getLogger(__name__)


_ENTITY_ID_RE = re.compile(r"\b(?:[A-Z]{2,}-?\d{2,}|[1-9]\d{1,5})\b")


@dataclass
class IntentResult:
    intent: str
    confidence: float
    entity_ids: list[str] = field(default_factory=list)
    tier_filter: Optional[str] = None
    urgency_filter: Optional[str] = None
    raw: str = ""


_groq_client: Optional[AsyncGroq] = None


def _get_groq() -> Optional[AsyncGroq]:
    global _groq_client
    if not settings.is_groq_configured():
        return None
    if _groq_client is None:
        _groq_client = AsyncGroq(api_key=settings.groq_api_key, max_retries=1, timeout=5.0)
    return _groq_client


_GREETING_WORDS = {
    "hi", "hello", "hey", "yo", "hola", "howdy", "sup", "morning", "afternoon", "evening",
    "good morning", "good afternoon", "good evening", "good night", "good day",
    "how are you", "how r u", "how do you do", "who are you", "what's up", "whats up",
}

_HELP_PATTERNS = (
    "what can you do", "what do you do", "how does this work", "how do you work",
    "help", "what should i ask", "what should i say", "what are you for",
    "your capabilities", "your features", "commands", "menu", "instructions",
    "how to use", "what can i ask",
)

_PIPELINE_PATTERNS = (
    "pipeline", "last run", "latest run", "analysis run", "autonomous run",
    "last analysis", "latest analysis",
)

_PIPELINE_DETAIL_PATTERNS = (
    "step", "steps", "how long", "duration", "model accuracy", "model performance",
    "f1 score", "f1", "auc", "feature importance", "risk driver", "risk drivers",
    "how many steps", "what steps", "ml model", "machine learning",
)

_OUTCOME_PATTERNS = (
    "churn", "churned", "churn rate", "how many.*churn", "retention",
    "attrition", "lost customer", "customer loss",
    "default", "defaulted", "fraud", "fraudulent",
    "readmit", "readmission", "readmitted",
    "delayed", "failed delivery", "missed",
    "outcome", "target rate", "positive rate",
)

_TREND_PATTERNS = (
    "trend", "over time", "history", "historical", "evolution",
    "changed over", "trajectory", "progress",
)

_COMPARE_PATTERNS = (
    "compare", "comparison", "diff", "difference", "changed since",
    "vs last", "versus last", "since last run", "delta",
    "what changed", "any changes",
)

_DATA_ACCESS_PATTERNS = (
    "database", "data base", "sql", "schema", "table", "tables", "raw data",
    "query my db", "query the db", "sql query", "run a query",
)

_OFF_TOPIC_PATTERNS = (
    "weather", "joke", "tell me a story", "are you real", "are you human",
    "your favorite", "what's your name", "who made you", "write code", "write me code",
    "code for", "python", "javascript", "translate", "summarize this",
    "news", "stock price", "bitcoin", "movie", "music", "song", "recipe",
    "what time", "what day", "what year",
)

_TIME_PRESSURE_PATTERNS = (
    "48 hour", "48h", "24 hour", "24h", "within", "deadline", "urgent", "asap",
    "due today", "by tomorrow", "time-sensitive",
)

_DASHBOARD_TOPIC_RE = re.compile(
    r"\b(dashboard|dashboards|chart|charts|visuali[sz]e|visuali[sz]ation|reports?)\b",
    re.I,
)

_DASHBOARD_CAPABILITY_RE = re.compile(
    r"^(can you|could you|do you|does it|are you able|is it possible|am i able)\b"
    r"|"
    r"\b(can you|could you|do you|are you able|is it possible)\b.{0,60}\b"
    r"(build|make|create|support)\b",
    re.I,
)

_DASHBOARD_IMPERATIVE_PHRASES = (
    "build me", "build a dashboard", "create a dashboard", "make me a dashboard",
    "create charts for", "create charts showing", "visualize ", "visualise ",
    "show me ", "make a report on", "report on ", "dashboard showing",
    "dashboard for", "charts for", "charts showing",
)

_DASHBOARD_GOAL_SIGNALS = (
    "churn", "revenue", "by month", "by region", "over time", "last ", "past ",
    "signups", "signup", "registration", "growth", "risk", "tier", "outcome",
    "fraud", "subscriber", "customer", "ticket", "support", "kpi", "metric",
    "showing ", "tracking ", "for the last", "per month", "per region",
    "broken down", "breakdown", "trend",
)

_DASHBOARD_CARD_REPLY_MARKERS = (
    "here are my answers",
    "please draft the plan",
    "please build this dashboard",
    "apply these changes",
    "change the plan:",
    "don't apply these",
)

# Tools whose presence on the prior assistant turn means the user is mid-flow
# (intake/plan shown, or changes proposed — still awaiting the user's next step).
_DASHBOARD_FOLLOWUP_TOOLS = frozenset({
    "start_dashboard_intake",
    "draft_dashboard_plan",
    "propose_dashboard_changes",
})

# Edit phrasing that, when a dashboard is already active, means "change THIS dashboard"
# (the topic regex also catches "chart"/"dashboard"). Gated by an active dashboard so
# it never hijacks ordinary questions just because edit verbs appear.
_DASHBOARD_EDIT_MARKERS = (
    "rename", "add a chart", "add another chart", "another chart", "add chart",
    "make it public", "make it private", "make this public", "make this private",
    "edit the dashboard", "update the dashboard", "to the dashboard",
    "from the dashboard", "swap", "replace the chart", "replace chart",
)


def is_dashboard_edit_request(message: str) -> bool:
    """True when the message asks to modify a dashboard (use only with an active dashboard)."""
    lower = (message or "").lower().strip()
    if not lower:
        return False
    if _DASHBOARD_TOPIC_RE.search(lower):
        return True
    return any(m in lower for m in _DASHBOARD_EDIT_MARKERS)


def is_dashboard_goal_ready(message: str) -> bool:
    """True when the user stated enough to auto-build a Studio dashboard."""
    lower = (message or "").lower().strip()
    if not lower:
        return False
    has_topic = bool(_DASHBOARD_TOPIC_RE.search(lower))
    has_goal_signal = any(p in lower for p in _DASHBOARD_GOAL_SIGNALS)
    has_imperative = any(p in lower for p in _DASHBOARD_IMPERATIVE_PHRASES)
    if has_imperative and (has_goal_signal or "showing" in lower or " for " in lower):
        return True
    if has_imperative and len(lower) > 45 and has_topic:
        return True
    if has_topic and has_goal_signal:
        return True
    return False


def is_dashboard_capability_question(message: str) -> bool:
    """True for yes/no capability asks about dashboards without a concrete goal."""
    lower = (message or "").lower().strip()
    if not lower or not _DASHBOARD_TOPIC_RE.search(lower):
        return False
    if is_dashboard_goal_ready(message):
        return False
    if _DASHBOARD_CAPABILITY_RE.search(lower):
        return True
    if lower.rstrip("?") in ("dashboard", "dashboards", "charts", "chart"):
        return True
    return False


def message_mentions_pipeline(message: str) -> bool:
    """True when the user is asking about autonomous pipeline / analysis runs."""
    lower = message.lower()
    return any(p in lower for p in _PIPELINE_PATTERNS)


def apply_pipeline_intent_override(intent: IntentResult, message: str) -> IntentResult:
    """Never short-circuit pipeline questions as off_topic."""
    if intent.intent == "off_topic" and message_mentions_pipeline(message):
        return IntentResult(
            intent="lookup_pipeline",
            confidence=max(intent.confidence, 0.85),
            entity_ids=intent.entity_ids,
            tier_filter=intent.tier_filter,
            urgency_filter=intent.urgency_filter,
            raw=intent.raw,
        )
    return intent


def apply_data_access_intent_override(intent: IntentResult, message: str) -> IntentResult:
    """Route SQL/schema questions to data_access instead of off_topic refusal."""
    if intent.intent != "off_topic":
        return intent
    lower = message.lower()
    if any(p in lower for p in _DATA_ACCESS_PATTERNS):
        return IntentResult(
            intent="data_access",
            confidence=max(intent.confidence, 0.88),
            entity_ids=intent.entity_ids,
            tier_filter=intent.tier_filter,
            urgency_filter=intent.urgency_filter,
            raw=intent.raw,
        )
    return intent


def apply_dashboard_intent_override(intent: IntentResult, message: str) -> IntentResult:
    """Route capability-only dashboard asks to discovery; require a goal before build."""
    if is_dashboard_capability_question(message):
        return IntentResult(
            intent="dashboard_discovery",
            confidence=max(intent.confidence, 0.92),
            entity_ids=intent.entity_ids,
            tier_filter=intent.tier_filter,
            urgency_filter=intent.urgency_filter,
            raw=intent.raw,
        )
    if intent.intent == "build_dashboard" and not is_dashboard_goal_ready(message):
        return IntentResult(
            intent="dashboard_discovery",
            confidence=max(intent.confidence, 0.85),
            entity_ids=intent.entity_ids,
            tier_filter=intent.tier_filter,
            urgency_filter=intent.urgency_filter,
            raw=intent.raw,
        )
    if intent.intent in ("help", "unknown") and is_dashboard_goal_ready(message):
        return IntentResult(
            intent="build_dashboard",
            confidence=max(intent.confidence, 0.88),
            entity_ids=intent.entity_ids,
            tier_filter=intent.tier_filter,
            urgency_filter=intent.urgency_filter,
            raw=intent.raw,
        )
    return intent


def _critical_count_negated(text: str) -> bool:
    """True when assistant said there are zero/no critical entities."""
    return bool(
        re.search(
            r"\b(?:zero|no|none|0)\s+critical\b|\bno\s+critical\b|\bcritical\s+flags?\s+and\s+came\s+out\s+with\s+zero",
            text,
        )
    )


def _assistant_tail(text: str, max_chars: int = 600) -> str:
    return text[-max_chars:] if len(text) > max_chars else text


def _heuristic_fallback(message: str) -> IntentResult:
    """Regex/keyword classifier used when Groq is unavailable. Conservative confidence."""
    ids = _ENTITY_ID_RE.findall(message)
    lower = message.lower().strip()
    tokens = set(re.findall(r"[a-z']+", lower))

    # Conversational intents first — these short-circuit before data classification.
    # Use length cap so "hi, what's our status?" doesn't get misclassified as greeting.
    if len(lower) <= 30:
        if lower in _GREETING_WORDS or tokens & {"hi", "hello", "hey", "yo", "hola", "howdy", "sup"}:
            return IntentResult("greeting", 0.85)
    if any(p in lower for p in _HELP_PATTERNS):
        return IntentResult("help", 0.8)
    if any(p in lower for p in _PIPELINE_DETAIL_PATTERNS):
        return IntentResult("lookup_pipeline_detail", 0.80)
    if message_mentions_pipeline(message):
        return IntentResult("lookup_pipeline", 0.85)
    if any(p in lower for p in _OUTCOME_PATTERNS):
        return IntentResult("lookup_outcome", 0.80, entity_ids=ids)
    if any(p in lower for p in _COMPARE_PATTERNS):
        return IntentResult("compare_runs", 0.80)
    if any(p in lower for p in _TREND_PATTERNS) and ids:
        return IntentResult("lookup_entity_trend", 0.80, entity_ids=ids)
    if any(p in lower for p in _OFF_TOPIC_PATTERNS):
        return IntentResult("off_topic", 0.75)

    # Data intents.
    if any(kw in lower for kw in ("draft", "compose", "write a message", "write an email")) and ids:
        return IntentResult("generate_draft", 0.7, entity_ids=ids)
    if any(kw in lower for kw in ("similar", "lookalike", "like ent-", "like ng-")) and ids:
        return IntentResult("find_similar", 0.7, entity_ids=ids)
    # Explanation/comparison wins over plain entity-id detection — "why is ENT-001 critical"
    # is a reasoning question, not a lookup.
    if any(kw in lower for kw in ("why", "explain", "compare ", "vs ", "versus", "trend", "drove", "caused")):
        return IntentResult("compare_or_explain", 0.65, entity_ids=ids)
    if "recommend" in lower or "action" in lower or "plate" in lower:
        urgency = "high" if any(p in lower for p in _TIME_PRESSURE_PATTERNS) else None
        return IntentResult("lookup_recommendations", 0.6, urgency_filter=urgency)
    if ids:
        return IntentResult("lookup_entity", 0.7, entity_ids=ids)
    if any(kw in lower for kw in ("overview", "snapshot", "status", "doing", "how are we")):
        return IntentResult("lookup_overview", 0.65)
    if any(kw in lower for kw in ("critical", "high risk", "high-risk", "at risk", "list", "show")):
        tier = "critical" if "critical" in lower else ("high" if "high" in lower else None)
        return IntentResult("lookup_entities", 0.6, tier_filter=tier)
    if _DASHBOARD_TOPIC_RE.search(lower):
        if is_dashboard_capability_question(message):
            return IntentResult("dashboard_discovery", 0.85)
        if is_dashboard_goal_ready(message):
            return IntentResult("build_dashboard", 0.75)
        return IntentResult("dashboard_discovery", 0.7)
    return IntentResult("unknown", 0.3)


async def classify_intent(
    user_message: str,
    *,
    convo_history: Optional[list[dict]] = None,
) -> Optional[IntentResult]:
    """Return an IntentResult, or None when the input is empty.

    Falls back to a deterministic heuristic when Groq is unavailable or errors out.
    """
    if not user_message or not user_message.strip():
        return None
    client = _get_groq()
    if client is None:
        return _heuristic_fallback(user_message)

    messages: list[dict] = [{"role": "system", "content": INTENT_CLASSIFIER_PROMPT}]
    if convo_history:
        for m in convo_history[-3:]:
            role = m.get("role")
            content = m.get("content")
            if role in ("user", "assistant") and isinstance(content, str):
                messages.append({"role": role, "content": content[:300]})
    messages.append({"role": "user", "content": user_message[:600]})

    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=settings.GROQ_LLM_MODEL_FAST,
                messages=messages,
                temperature=0.1,
                max_tokens=200,
                response_format={"type": "json_object"},
            ),
            timeout=5.0,
        )
        raw = (response.choices[0].message.content or "").strip()
        data = json.loads(raw)
        if not isinstance(data, dict):
            return _heuristic_fallback(user_message)
        intent = str(data.get("intent") or "unknown")
        try:
            confidence = float(data.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        eids_raw = data.get("entity_ids") or []
        if not isinstance(eids_raw, list):
            eids_raw = []
        entity_ids = [str(e).strip() for e in eids_raw if e]
        tier = data.get("tier_filter")
        urgency = data.get("urgency_filter")
        return IntentResult(
            intent=intent,
            confidence=max(0.0, min(1.0, confidence)),
            entity_ids=entity_ids,
            tier_filter=str(tier) if tier else None,
            urgency_filter=str(urgency) if urgency else None,
            raw=raw,
        )
    except Exception as exc:
        logger.debug("[intent] classify failed, using heuristic: %s", exc)
        return _heuristic_fallback(user_message)


# ── Routing tables ─────────────────────────────────────────────────────────

# Conversational intents bypass the ReAct loop entirely — handled by
# agent_service._conversational_reply with no tool calls.
CONVERSATIONAL_INTENTS = frozenset({
    "greeting", "help", "data_access", "off_topic", "unknown", "dashboard_discovery",
})

# Tool subset the ReAct loop should see when this intent fires.
# None = no prefilter (give the agent all tools).
INTENT_TOOLS: dict[str, Optional[tuple[str, ...]]] = {
    "greeting": None,
    "help": None,
    "off_topic": None,
    "lookup_pipeline": ("get_pipeline_status",),
    "lookup_pipeline_detail": ("get_pipeline_detail", "get_pipeline_status"),
    "lookup_overview": ("get_overview",),
    "lookup_entity": ("get_entity_detail", "find_similar_entities"),
    "lookup_entities": ("get_entities", "get_overview"),
    "lookup_recommendations": ("get_recommendations", "get_entity_detail"),
    "lookup_outcome": ("get_outcome_analysis", "get_overview"),
    "lookup_entity_trend": ("get_entity_trend", "get_entity_detail"),
    "compare_runs": ("compare_pipeline_runs", "get_pipeline_status"),
    "find_similar": ("find_similar_entities", "get_entity_detail"),
    "generate_draft": ("generate_action_draft", "get_entity_detail"),
    "build_dashboard": (
        "start_dashboard_intake",
        "draft_dashboard_plan",
        "build_dashboard_from_plan",
        "propose_dashboard_changes",
        "apply_dashboard_changes",
    ),
    "compare_or_explain": None,
    "unknown": None,
}


# When confidence >= threshold AND params are sufficient, skip the ReAct loop
# entirely and call this single tool directly.
_FASTPATH_TOOL: dict[str, str] = {
    "lookup_pipeline": "get_pipeline_status",
    "lookup_pipeline_detail": "get_pipeline_detail",
    "lookup_overview": "get_overview",
    "lookup_entity": "get_entity_detail",
    "lookup_recommendations": "get_recommendations",
    "lookup_outcome": "get_outcome_analysis",
    "lookup_entity_trend": "get_entity_trend",
    "compare_runs": "compare_pipeline_runs",
    "find_similar": "find_similar_entities",
    "generate_draft": "generate_action_draft",
}


def build_fastpath_args(
    intent: IntentResult,
    user_message: str | None = None,
) -> Optional[tuple[str, dict]]:
    """For a high-confidence intent, return (tool_name, args) to execute directly.

    Returns None when fast-path isn't applicable (intent doesn't map, or required
    extracted params are missing). Caller should fall back to the prefilter path.
    """
    tool = _FASTPATH_TOOL.get(intent.intent)
    if not tool:
        return None
    if tool == "get_pipeline_status":
        return tool, {}
    if tool == "get_pipeline_detail":
        return tool, {}
    if tool == "get_overview":
        return tool, {}
    if tool == "get_outcome_analysis":
        return tool, {}
    if tool == "compare_pipeline_runs":
        return tool, {}
    if tool == "get_recommendations":
        args: dict = {"limit": 15}
        lower = (user_message or "").lower()
        if intent.urgency_filter:
            args["urgency"] = intent.urgency_filter
        elif any(p in lower for p in _TIME_PRESSURE_PATTERNS):
            args["urgency"] = "high"
        return tool, args
    if tool in ("get_entity_detail", "find_similar_entities", "get_entity_trend"):
        if not intent.entity_ids:
            return None
        return tool, {"entity_id": intent.entity_ids[0]}
    if tool == "generate_action_draft":
        if not intent.entity_ids:
            return None
        return tool, {"entity_id": intent.entity_ids[0], "action_type": "message"}
    return None


_AFFIRMATIVE_TOKENS = frozenset({
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "please", "do it",
    "go ahead", "sounds good", "let's do it", "lets do it", "y",
})

_VAGUE_FOLLOWUP_PHRASES = (
    "tell me about it", "tell me more", "more about that", "what about it",
    "go on", "continue", "and?", "so?",
)


def _last_assistant_text(conversation_messages: list[dict]) -> str:
    for msg in reversed(conversation_messages):
        if msg.get("role") == "assistant":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content
    return ""


def resolve_followup_query(
    conversation_messages: list[dict],
    user_message: str,
) -> str | None:
    """Rewrite short affirmations / vague follow-ups into a concrete data query.

    Extracts the specific offer or action from the assistant's last message
    and rewrites the user's 'yes' / 'sure' / 'tell me more' into the
    concrete query that fulfills that offer.
    """
    if not user_message or not conversation_messages:
        return None
    lower = user_message.lower().strip()
    assistant = _last_assistant_text(conversation_messages).lower()

    is_affirmative = (
        lower in _AFFIRMATIVE_TOKENS
        or lower.startswith("yes ")
        or lower == "yes"
    )
    is_vague = (
        lower in _VAGUE_FOLLOWUP_PHRASES
        or (lower.startswith("okay") and len(lower) < 40)
        or (len(lower) < 20 and "about it" in lower)
    )
    if not is_affirmative and not is_vague:
        return None

    # Priority 1: The assistant offered to dig into a specific entity.
    # Patterns like "dig into 628", "zoom in on 1613", "pull up 42".
    entity_offer = re.search(
        r"(?:dig into|zoom in on|pull up|look at|check on|details (?:for|on))\s+"
        r"(?:entity\s+|customer\s+|#)?(\d{1,6}|[A-Z]{2,}-?\d{2,})",
        assistant,
    )
    if entity_offer:
        return f"tell me about entity {entity_offer.group(1)}"

    # Priority 2: The assistant mentioned specific entities by ID in an offer.
    # e.g. "Want me to pull 628's full profile?"
    id_in_offer = re.search(
        r"(?:want me to|shall i|i can|let me)\s+.{0,40}?\b(\d{2,6}|[A-Z]{2,}-?\d{2,})\b",
        assistant,
    )
    if id_in_offer:
        return f"tell me about entity {id_in_offer.group(1)}"

    # Priority 3: The assistant offered to pull a specific list.
    if "top 3" in assistant or "top three" in assistant:
        return "show the top 3 at-risk entities"
    if re.search(r"pull\s+(?:the\s+)?(?:broader\s+)?overview", assistant):
        return "what's our status?"
    if re.search(r"pull\s+(?:the\s+)?(?:full\s+)?list", assistant):
        return "show all high-risk entities"

    # Priority 4: Topic-level matching (prefer tail where the offer usually lives).
    tail = _assistant_tail(assistant)
    if (
        ("high-risk" in tail or "high risk" in tail or re.search(r"\b\d+\s+high(?:-risk)?", tail))
        and ("entit" in tail or "customer" in tail or "recommendation" in tail)
    ):
        return "show all high-risk entities"
    if (
        "critical" in tail
        and ("entit" in tail or "customer" in tail)
        and not _critical_count_negated(tail)
    ):
        return "show critical entities"
    if "high" in tail and "risk" in tail:
        return "show all high-risk entities"
    if "recommendation" in assistant or "action" in assistant:
        return "what recommendations can you give me?"
    if "pipeline" in assistant or "last run" in assistant or "latest run" in assistant:
        return "what was my latest pipeline run about?"
    if "similar" in assistant or "lookalike" in assistant:
        return "find similar entities"
    if "status" in assistant or "overview" in assistant or "entity list" in assistant:
        return "what's our status?"

    # Default for uncategorized affirmatives: overview.
    if is_affirmative or is_vague:
        return "what's our status?"
    return None


def is_dashboard_followup(conversation_messages: list[dict], user_message: str) -> bool:
    """True when the user is mid dashboard discovery/intake/preview/edit flow.

    This turn must reach the dashboard tools (ReAct), not a conversational
    short-circuit or the wrong intent. Signals: the frontend cards reply with
    fixed phrases; the prior assistant turn ran a dashboard intake/plan/propose
    tool; or the prior turn was a dashboard discovery reply and the user answered
    with a substantive goal.
    """
    lower = (user_message or "").lower().strip()
    if any(p in lower for p in _DASHBOARD_CARD_REPLY_MARKERS):
        return True

    last_assistant = next(
        (m for m in reversed(conversation_messages or []) if m.get("role") == "assistant"),
        None,
    )
    if last_assistant is None:
        return False

    called = last_assistant.get("tools_called") or []
    if any(t in _DASHBOARD_FOLLOWUP_TOOLS for t in called):
        return True
    arts = last_assistant.get("artifacts") or {}
    if isinstance(arts, dict) and any(k in _DASHBOARD_FOLLOWUP_TOOLS for k in arts):
        return True

    # Bridge: the assistant just ran a dashboard discovery reply (asked what to
    # track) and the user answered with a goal rather than a new capability ask.
    ctx = last_assistant.get("tool_context") or {}
    if isinstance(ctx, dict) and ctx.get("dashboard_discovery"):
        if len(lower) >= 6 and not is_dashboard_capability_question(user_message):
            return True
    return False


def filter_tools_by_intent(
    all_tools: list[dict], intent: IntentResult,
) -> list[dict]:
    """Return only the tools allowed for this intent (or all when no prefilter)."""
    allowed = INTENT_TOOLS.get(intent.intent)
    if not allowed:
        return all_tools
    return [t for t in all_tools if t.get("name") in allowed]
