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


_ENTITY_ID_RE = re.compile(r"\b[A-Z]{2,}-?\d{2,}\b")


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

_OFF_TOPIC_PATTERNS = (
    "weather", "joke", "tell me a story", "are you real", "are you human",
    "your favorite", "what's your name", "who made you", "write code", "write me code",
    "code for", "python", "javascript", "sql to", "translate", "summarize this",
    "news", "stock price", "bitcoin", "movie", "music", "song", "recipe",
    "what time", "what day", "what year",
)


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
    if ids:
        return IntentResult("lookup_entity", 0.7, entity_ids=ids)
    if any(kw in lower for kw in ("overview", "snapshot", "status", "doing", "how are we")):
        return IntentResult("lookup_overview", 0.65)
    if "recommend" in lower or "action" in lower or "plate" in lower:
        return IntentResult("lookup_recommendations", 0.6)
    if any(kw in lower for kw in ("critical", "high risk", "high-risk", "at risk", "list", "show")):
        tier = "critical" if "critical" in lower else ("high" if "high" in lower else None)
        return IntentResult("lookup_entities", 0.6, tier_filter=tier)
    if any(
        kw in lower
        for kw in (
            "dashboard",
            "build a chart",
            "build me a chart",
            "visuali",
            "visualize",
            "visualise",
            "report on",
            "make a report",
            "create charts",
        )
    ):
        return IntentResult("build_dashboard", 0.75)
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
CONVERSATIONAL_INTENTS = frozenset({"greeting", "help", "off_topic"})

# Tool subset the ReAct loop should see when this intent fires.
# None = no prefilter (give the agent all tools).
INTENT_TOOLS: dict[str, Optional[tuple[str, ...]]] = {
    "greeting": None,
    "help": None,
    "off_topic": None,
    "lookup_overview": ("get_overview",),
    "lookup_entity": ("get_entity_detail", "find_similar_entities"),
    "lookup_entities": ("get_entities", "get_overview"),
    "lookup_recommendations": ("get_recommendations", "get_entity_detail"),
    "find_similar": ("find_similar_entities", "get_entity_detail"),
    "generate_draft": ("generate_action_draft", "get_entity_detail"),
    "compare_or_explain": None,
    "build_dashboard": ("build_custom_dashboard",),
    "unknown": None,
}


# When confidence >= threshold AND params are sufficient, skip the ReAct loop
# entirely and call this single tool directly.
_FASTPATH_TOOL: dict[str, str] = {
    "lookup_overview": "get_overview",
    "lookup_entity": "get_entity_detail",
    "lookup_recommendations": "get_recommendations",
    "find_similar": "find_similar_entities",
    "generate_draft": "generate_action_draft",
    "build_dashboard": "build_custom_dashboard",
}


def build_fastpath_args(
    intent: IntentResult, *, user_message: str = ""
) -> Optional[tuple[str, dict]]:
    """For a high-confidence intent, return (tool_name, args) to execute directly.

    Returns None when fast-path isn't applicable (intent doesn't map, or required
    extracted params are missing). Caller should fall back to the prefilter path.
    """
    tool = _FASTPATH_TOOL.get(intent.intent)
    if not tool:
        return None
    if tool == "get_overview":
        return tool, {}
    if tool == "get_recommendations":
        args: dict = {}
        if intent.urgency_filter:
            args["urgency"] = intent.urgency_filter
        return tool, args
    if tool in ("get_entity_detail", "find_similar_entities"):
        if not intent.entity_ids:
            return None
        return tool, {"entity_id": intent.entity_ids[0]}
    if tool == "generate_action_draft":
        if not intent.entity_ids:
            return None
        return tool, {"entity_id": intent.entity_ids[0], "action_type": "message"}
    if tool == "build_custom_dashboard":
        goal = user_message.strip()
        if not goal:
            return None
        return tool, {"goal": goal, "max_charts": 4}
    return None


def filter_tools_by_intent(
    all_tools: list[dict], intent: IntentResult,
) -> list[dict]:
    """Return only the tools allowed for this intent (or all when no prefilter)."""
    allowed = INTENT_TOOLS.get(intent.intent)
    if not allowed:
        return all_tools
    return [t for t in all_tools if t.get("name") in allowed]
