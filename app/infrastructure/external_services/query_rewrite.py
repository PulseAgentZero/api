"""Pre-retrieval: rewrite an entity record into a focused retrieval query.

Uses Groq's fast model. Skipping is safe: callers fall back to raw query text
when this returns None or raises.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from groq import AsyncGroq

from app.config.settings import settings

logger = logging.getLogger(__name__)

_REWRITE_SYSTEM = (
    "You compress an at-risk entity record into ONE focused retrieval query. "
    "The query will be used to find behaviourally similar entities in a vector "
    "store. Emphasize: dominant risk signal, magnitude, and direction (rising / "
    "falling / zero). Drop boilerplate. 25 words max. Output the query line only."
)

_CACHE: dict[str, str] = {}
_CACHE_MAX = 512


_client: AsyncGroq | None = None


def _get_client() -> AsyncGroq | None:
    global _client
    if not settings.is_groq_configured():
        return None
    if _client is None:
        _client = AsyncGroq(api_key=settings.groq_api_key, max_retries=2, timeout=8.0)
    return _client


def _cache_key(entity: dict) -> str:
    return json.dumps(
        {
            "eid": entity.get("entity_id"),
            "score": entity.get("risk_score"),
            "signals": entity.get("signal_values") or {},
        },
        sort_keys=True,
        default=str,
    )


async def rewrite_entity_query(entity: dict, fallback: str) -> str:
    """Return a compressed retrieval query, or the fallback on any failure."""
    if not settings.RAG_ENABLE_QUERY_REWRITE:
        return fallback
    client = _get_client()
    if client is None:
        return fallback

    key = _cache_key(entity)
    cached = _CACHE.get(key)
    if cached:
        return cached

    user_msg = f"Entity record:\n{json.dumps(entity, default=str)[:1500]}"
    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=settings.GROQ_LLM_MODEL_FAST,
                messages=[
                    {"role": "system", "content": _REWRITE_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.1,
                max_tokens=80,
            ),
            timeout=6.0,
        )
        text = (response.choices[0].message.content or "").strip()
        # Take only the first non-empty line in case the model adds preamble.
        for line in text.splitlines():
            stripped = line.strip(" -*\t")
            if stripped:
                text = stripped
                break
        if not text:
            return fallback
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = text
        return text
    except Exception as exc:
        logger.debug("[QueryRewrite] failed, using fallback: %s", exc)
        return fallback
