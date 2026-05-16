"""Procedural memory: org-level learnings that survive across pipeline runs.

Stores routine-level facts (e.g. "for fintech orgs with >500 entities, use
decomposed retrieval"; "model accuracy crossed 70% after enabling rerank").
Reuses the per-org `_memory` Qdrant collection with `kind='procedural'` and
`user_id='__org__'` sentinel — separate from the per-user episodic store but
in the same collection for retention/prune simplicity."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from uuid import UUID

from groq import AsyncGroq

from app.config.settings import settings
from app.infrastructure.external_services.embeddings import embedding_service
from app.infrastructure.external_services.qdrant import (
    QdrantService,
    SearchResult,
    memory_point_id,
)

logger = logging.getLogger(__name__)


_ORG_USER_SENTINEL = "__org__"
_KIND = "procedural"


_EXTRACT_SYSTEM = (
    "You distill ONE durable, generalizable learning from a Pulse pipeline run. "
    "Examples worth committing: which retrieval config worked, which risk-tier "
    "patterns dominated, which agent decisions paid off, which inputs caused "
    "regressions. Skip routine successes that add no new knowledge.\n"
    "Return ONLY JSON: "
    '{"commit": bool, "content": "single sentence stating the learning", '
    '"importance": float 0..1}.'
)


_groq_client: AsyncGroq | None = None


def _get_groq() -> AsyncGroq | None:
    global _groq_client
    if not settings.is_groq_configured():
        return None
    if _groq_client is None:
        _groq_client = AsyncGroq(api_key=settings.groq_api_key, max_retries=1, timeout=8.0)
    return _groq_client


def _enabled() -> bool:
    return (
        settings.CONV_MEMORY_ENABLED
        and settings.is_voyage_configured()
        and settings.is_qdrant_configured()
    )


async def commit_learning(
    org_id: UUID | str,
    content: str,
    *,
    importance: float = 0.7,
    source: str = "pipeline_run",
) -> dict | None:
    """Embed and upsert a single procedural learning. Returns payload or None on failure."""
    if not _enabled() or not (content or "").strip():
        return None
    try:
        qdrant = QdrantService()
        await qdrant.ensure_memory_collection(str(org_id))
        vector = await embedding_service.embed_query(content)
        payload: dict[str, Any] = {
            "user_id": _ORG_USER_SENTINEL,
            "kind": _KIND,
            "content": content.strip(),
            "importance": float(importance),
            "source": source,
            "embedded_at": time.time(),
        }
        pid = memory_point_id(_ORG_USER_SENTINEL + ":" + str(org_id), payload["content"])
        await qdrant.upsert_memory(str(org_id), [(pid, vector, payload)])
        logger.info(
            "[procedural] committed learning org=%s importance=%.2f content=%r",
            org_id, importance, content[:80],
        )
        return payload
    except Exception as exc:
        logger.warning("[procedural] commit failed (non-fatal): %s", exc)
        return None


async def recall_learnings(
    org_id: UUID | str,
    query_text: str,
    *,
    top_k: int | None = None,
) -> list[SearchResult]:
    """Return top-K procedural learnings semantically related to query_text."""
    if not _enabled() or not (query_text or "").strip():
        return []
    k = top_k or settings.CONV_MEMORY_RECALL_K
    try:
        qdrant = QdrantService()
        vector = await embedding_service.embed_query(query_text)
        return await qdrant.search_memory(
            str(org_id), vector, kind=_KIND, limit=k,
        )
    except Exception as exc:
        logger.debug("[procedural] recall failed (returning empty): %s", exc)
        return []


def format_learnings_for_prompt(learnings: list[SearchResult]) -> str:
    if not learnings:
        return ""
    lines: list[str] = []
    for m in learnings:
        content = (m.payload or {}).get("content", "").strip()
        if content:
            lines.append(f"- {content}")
    if not lines:
        return ""
    return (
        "Procedural learnings from past pipeline runs for this org "
        "(use to bias configuration and prioritization):\n"
        + "\n".join(lines)
        + "\n"
    )


async def extract_and_commit_from_run(
    org_id: UUID | str,
    run_summary: dict,
) -> dict | None:
    """Run a Groq reflection over a pipeline run summary and commit if worthwhile."""
    if not _enabled():
        return None
    client = _get_groq()
    if client is None:
        return None
    user_msg = "Pipeline run summary:\n" + json.dumps(run_summary, default=str)[:1500]
    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=settings.GROQ_LLM_MODEL_FAST,
                messages=[
                    {"role": "system", "content": _EXTRACT_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.0,
                max_tokens=180,
            ),
            timeout=8.0,
        )
        raw = (response.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw[raw.find("{") :]
        decision = json.loads(raw)
    except Exception as exc:
        logger.debug("[procedural] extract failed: %s", exc)
        return None

    if not isinstance(decision, dict) or not decision.get("commit"):
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
    return await commit_learning(org_id, content, importance=importance, source="run_reflection")
