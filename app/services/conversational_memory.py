"""Semantic episodic memory for the conversational agent.

Reflects on each turn via Groq fast model, decides if it's worth committing,
embeds via Voyage, and stores in a per-org Qdrant memory collection. Recalls
top-K relevant memories for the next user query. Degrades gracefully when
any dependency is unconfigured."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from uuid import UUID

from groq import AsyncGroq

from app.config.settings import settings
from app.infrastructure.database.models.user import User
from app.infrastructure.external_services.embeddings import embedding_service
from app.infrastructure.external_services.qdrant import (
    QdrantService,
    SearchResult,
    memory_point_id,
)

logger = logging.getLogger(__name__)


from app.agents.prompts.memory import REFLECT_PROMPT, SUMMARIZE_PROMPT


_groq_client: AsyncGroq | None = None


def _get_groq() -> AsyncGroq | None:
    global _groq_client
    if not settings.is_groq_configured():
        return None
    if _groq_client is None:
        _groq_client = AsyncGroq(api_key=settings.groq_api_key, max_retries=1, timeout=6.0)
    return _groq_client


def _can_use_memory() -> bool:
    return (
        settings.CONV_MEMORY_ENABLED
        and settings.is_voyage_configured()
        and settings.is_qdrant_configured()
    )


async def _reflect(user_message: str, assistant_reply: str) -> dict | None:
    """Ask Groq fast model whether this turn produced a durable memory item."""
    client = _get_groq()
    if client is None:
        return None
    user_msg = (
        f"User: {user_message[:600]}\n"
        f"Assistant: {assistant_reply[:600]}"
    )
    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=settings.GROQ_LLM_MODEL_FAST,
                messages=[
                    {"role": "system", "content": REFLECT_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.0,
                max_tokens=180,
            ),
            timeout=6.0,
        )
        raw = (response.choices[0].message.content or "").strip()
        # Strip code fences if the model added any.
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw[raw.find("{") :]
        decision = json.loads(raw)
        if not isinstance(decision, dict):
            return None
        return decision
    except Exception as exc:
        logger.debug("[conv_memory] reflect failed: %s", exc)
        return None


async def reflect_and_commit(
    current_user: User,
    user_message: str,
    assistant_reply: str,
) -> dict | None:
    """Decide + commit a memory item from one chat turn. Returns the committed payload or None."""
    if not _can_use_memory() or not user_message.strip() or not assistant_reply.strip():
        return None

    decision = await _reflect(user_message, assistant_reply)
    if not decision or not decision.get("commit"):
        return None
    content = str(decision.get("content") or "").strip()
    if not content:
        return None
    try:
        importance = float(decision.get("importance") or 0.0)
    except (TypeError, ValueError):
        importance = 0.0
    if importance < settings.CONV_MEMORY_IMPORTANCE_THRESHOLD:
        return None
    kind = str(decision.get("kind") or "episodic")

    org_id = str(current_user.org_id)
    user_id = str(current_user.id)

    try:
        qdrant = QdrantService()
        await qdrant.ensure_memory_collection(org_id)
        vector = await embedding_service.embed_query(content)
        payload: dict[str, Any] = {
            "user_id": user_id,
            "kind": kind,
            "content": content,
            "importance": importance,
            "source": "reflection",
            "embedded_at": time.time(),
        }
        pid = memory_point_id(user_id, content)
        await qdrant.upsert_memory(org_id, [(pid, vector, payload)])
        logger.info(
            "[conv_memory] committed memory user=%s importance=%.2f content=%r",
            user_id, importance, content[:80],
        )
        return payload
    except Exception as exc:
        logger.warning("[conv_memory] commit failed (non-fatal): %s", exc)
        return None


async def recall(
    current_user: User,
    query_text: str,
    *,
    top_k: int | None = None,
    min_score: float | None = None,
    kind: str | None = None,
) -> list[SearchResult]:
    """Return top-K semantically relevant memories for this user, filtered by min score.

    `kind` lets callers narrow to a specific memory type (e.g. 'conversation_summary'
    for the handoff block). When None, all per-user memories are eligible.
    """
    if not _can_use_memory() or not query_text.strip():
        return []
    k = top_k or settings.CONV_MEMORY_RECALL_K
    threshold = settings.CONV_MEMORY_MIN_RECALL_SCORE if min_score is None else min_score
    org_id = str(current_user.org_id)
    user_id = str(current_user.id)

    try:
        qdrant = QdrantService()
        vector = await embedding_service.embed_query(query_text)
        results = await qdrant.search_memory(
            org_id, vector, user_id=user_id, kind=kind, limit=k,
        )
        if threshold > 0:
            results = [r for r in results if (r.score or 0.0) >= threshold]
        return results
    except Exception as exc:
        logger.debug("[conv_memory] recall failed (returning empty): %s", exc)
        return []


def format_recalled_for_prompt(memories: list[SearchResult]) -> str:
    """Render recalled memories as a system-prompt block. Empty string when none."""
    if not memories:
        return ""
    lines = []
    for m in memories:
        content = (m.payload or {}).get("content", "").strip()
        if not content:
            continue
        score = round(float(m.score or 0.0), 3)
        lines.append(f"- {content} (relevance={score})")
    if not lines:
        return ""
    return (
        "Relevant memories from past conversations with this user "
        "(use to bias your reasoning; do not parrot back unless asked):\n"
        + "\n".join(lines)
        + "\n"
    )


def format_handoff_for_prompt(summaries: list[SearchResult]) -> str:
    """Render conversation summaries as a 'recent sessions' handoff block."""
    if not summaries:
        return ""
    lines: list[str] = []
    for m in summaries:
        content = (m.payload or {}).get("content", "").strip()
        if content:
            lines.append(f"- {content}")
    if not lines:
        return ""
    return (
        "Context from this user's recent sessions (use only if the question "
        "implies continuity; do not pre-emptively reference):\n"
        + "\n".join(lines)
        + "\n"
    )


def _format_transcript_for_summary(messages: list[dict]) -> str:
    """Render the last N messages of a chat thread for the summarizer prompt."""
    lines: list[str] = []
    for m in messages[-10:]:
        role = m.get("role")
        content = m.get("content")
        if not isinstance(content, str):
            content = json.dumps(content, default=str)[:300]
        if role and content:
            lines.append(f"{role}: {content[:400]}")
    return "\n".join(lines)


async def summarize_conversation(
    current_user: User,
    messages: list[dict],
) -> str | None:
    """Distil a chat thread to one sentence and commit as kind='conversation_summary'."""
    if not _can_use_memory() or not messages:
        return None
    client = _get_groq()
    if client is None:
        return None
    transcript = _format_transcript_for_summary(messages)
    if not transcript:
        return None
    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=settings.GROQ_LLM_MODEL_FAST,
                messages=[
                    {"role": "system", "content": SUMMARIZE_PROMPT},
                    {"role": "user", "content": transcript},
                ],
                temperature=0.0,
                max_tokens=120,
            ),
            timeout=6.0,
        )
        summary = (response.choices[0].message.content or "").strip()
        if not summary:
            return None

        org_id = str(current_user.org_id)
        user_id = str(current_user.id)
        qdrant = QdrantService()
        await qdrant.ensure_memory_collection(org_id)
        vector = await embedding_service.embed_query(summary)
        # Distinct namespace so summaries don't collide with fact memories.
        pid = memory_point_id(user_id + ":summary", summary)
        await qdrant.upsert_memory(org_id, [(
            pid, vector, {
                "user_id": user_id,
                "kind": "conversation_summary",
                "content": summary,
                "importance": 0.75,
                "source": "idle_summary",
                "embedded_at": time.time(),
            },
        )])
        logger.info("[conv_memory] summarized conversation user=%s: %r", user_id, summary[:80])
        return summary
    except Exception as exc:
        logger.debug("[conv_memory] summarize failed: %s", exc)
        return None


async def prune(org_id: UUID | str) -> int:
    """Periodic cleanup: drop low-importance and aged-out memories."""
    if not _can_use_memory():
        return 0
    try:
        qdrant = QdrantService()
        return await qdrant.prune_memory(
            str(org_id),
            importance_below=settings.CONV_MEMORY_IMPORTANCE_THRESHOLD,
            older_than_days=settings.CONV_MEMORY_RETENTION_DAYS,
        )
    except Exception as exc:
        logger.warning("[conv_memory] prune failed (non-fatal): %s", exc)
        return 0
