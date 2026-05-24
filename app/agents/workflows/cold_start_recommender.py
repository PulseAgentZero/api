"""Cold-start / persona-driven recommendation agent.

Distinct from :class:`app.agents.workflows.recommendation_agent.RecommendationAgent`
(the batch pipeline agent that generates next-best-actions per profiled entity).
This agent handles the *interactive* recommendation flow:

- **warm-start** — known ``user_id``, history-vector retrieval
- **cold-start** — only a free-text persona
- **multi-turn** — ``conversation_id`` + ``follow_up`` refinement
- **cross-domain** — ``dataset=goodreads``

Runs on Entivia's :class:`BaseAgent` (ReAct loop, Anthropic primary + Groq
fallback, JSON validation).

The retrieval layer (embeddings, vector store, history lookup) is **lazy-loaded
from the hackathon adapters** so this module has no static dependency on
``hackathon/``. A future refactor will move those adapters into
``app.infrastructure.embeddings`` and ``app.infrastructure.vector_store`` so the
agent can serve any tenant; until then, callers from production must supply a
persona-only request (cold-start mode), which doesn't touch the hackathon DB.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from app.agents.base import BaseAgent, LLMProvider
from app.agents.observability import agent_run_meta, start_timer
from app.agents.tools.base import Tool, ToolParam

logger = logging.getLogger(__name__)
_PROMPTS = Path(__file__).resolve().parent / "prompts" / "simulation"

_CANDIDATE_FLOOR = 30
_CANDIDATE_MULTIPLIER = 4
_CANDIDATE_CEILING = 50
_RERANK_CONTEXT_ITEMS = 40
_RERANK_CONTEXT_BUDGET_CHARS = 8_000
_MAX_HISTORY_ROWS = 200
_TOOL_CANDIDATE_DEFAULT = 40


class _ConversationStore:
    """Tiny in-memory store for multi-turn follow-ups (per-process, demo scope)."""

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}

    def shown_ids(self, conversation_id: str | None) -> list[str]:
        if not conversation_id or conversation_id not in self._sessions:
            return []
        return list(self._sessions[conversation_id].get("shown_ids", []))

    def get(self, conversation_id: str) -> dict[str, Any] | None:
        """Return the stored session dict (dataset, shown_ids, turns) or None."""
        session = self._sessions.get(conversation_id)
        if session is None:
            return None
        return {
            "dataset": session.get("dataset"),
            "shown_ids": list(session.get("shown_ids", [])),
            "turns": int(session.get("turns", 0)),
        }

    def remember(
        self,
        conversation_id: str | None,
        dataset: str,
        new_ids: list[str],
    ) -> str:
        cid = conversation_id or str(uuid.uuid4())
        existing = self._sessions.get(cid, {})
        existing_ids = existing.get("shown_ids", [])
        self._sessions[cid] = {
            "dataset": dataset,
            "shown_ids": list({*existing_ids, *new_ids}),
            "turns": int(existing.get("turns", 0)) + 1,
        }
        return cid


def get_conversation_store() -> _ConversationStore:
    """Public accessor so other modules (e.g. hackathon system router) can read history."""
    return _CONVERSATIONS


_CONVERSATIONS = _ConversationStore()


# ── Lazy adapters: keep `app/` free of static `hackathon/` imports ──────────


def _embed_persona_text_sync(text: str) -> list[float]:
    from hackathon.core.embeddings import embed_query, pseudo_embed

    if not text:
        return pseudo_embed("")
    return embed_query(text)


async def _embed_persona_text(text: str) -> list[float]:
    return _embed_persona_text_sync(text)


async def _user_vector_or_synthesize(user_id: str) -> tuple[list[float], list[dict[str, Any]]]:
    from hackathon.core.repository import fetch_user_history, get_user_vector

    history = await fetch_user_history(user_id, _MAX_HISTORY_ROWS)
    vector = await get_user_vector(user_id)
    if vector is None:
        summary = " ".join(f"{h['name']} ({h['stars']} stars)" for h in history[:20])
        vector = await _embed_persona_text(summary or user_id)
    return vector, history


def _vector_store():
    from hackathon.core.vector_store import vector_store

    return vector_store


def _embedding_backend_label() -> str:
    from hackathon.config import EMBEDDING_BACKEND

    return EMBEDDING_BACKEND


# ── Agent ────────────────────────────────────────────────────────────────────


class ColdStartRecommendationAgent(BaseAgent):
    """ReAct agent that turns ANN candidates into a ranked, explained list."""

    def __init__(self) -> None:
        super().__init__(
            name="recommender",
            provider=LLMProvider.ANTHROPIC,
            fallback_enabled=True,
        )
        self._register_tools()

    def _register_tools(self) -> None:
        self.registry.register(self._tool_fetch_user_history())
        self.registry.register(self._tool_fetch_item())
        self.registry.register(self._tool_ann_search())

    def _tool_fetch_user_history(self) -> Tool:
        async def execute(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
            from hackathon.core.repository import fetch_user_history

            return await fetch_user_history(user_id, limit=limit)

        return Tool(
            name="fetch_user_history",
            description="Recent reviews by this user (training split only).",
            parameters=[
                ToolParam("user_id", "string", "User id", required=True),
                ToolParam("limit", "integer", "Max rows", required=False),
            ],
            execute=execute,
        )

    def _tool_fetch_item(self) -> Tool:
        async def execute(item_id: str) -> dict[str, Any]:
            from hackathon.core.repository import fetch_item

            data = await fetch_item(item_id)
            return data or {"error": f"Unknown item: {item_id}"}

        return Tool(
            name="fetch_item",
            description="Item metadata by id.",
            parameters=[ToolParam("item_id", "string", "Item id", required=True)],
            execute=execute,
        )

    def _tool_ann_search(self) -> Tool:
        async def execute(
            dataset: str = "yelp",
            k: int = _TOOL_CANDIDATE_DEFAULT,
            user_id: str | None = None,
            persona: str | None = None,
            exclude_item_ids: list[str] | None = None,
        ) -> list[dict[str, Any]]:
            if user_id:
                vector, _ = await _user_vector_or_synthesize(user_id)
            elif persona:
                vector = await _embed_persona_text(persona)
            else:
                return [{"error": "Provide user_id or persona"}]
            return await _vector_store().search(
                vector,
                k=k,
                dataset=dataset,
                exclude_item_ids=exclude_item_ids or [],
            )

        return Tool(
            name="ann_search_items",
            description="Vector retrieval of candidate items. Pass user_id OR persona.",
            parameters=[
                ToolParam("dataset", "string", "yelp or goodreads", required=False),
                ToolParam("k", "integer", "Candidate pool size", required=False),
                ToolParam("user_id", "string", "Known user", required=False),
                ToolParam("persona", "string", "Cold-start persona text", required=False),
                ToolParam(
                    "exclude_item_ids",
                    "array",
                    "Ids to exclude (multi-turn)",
                    required=False,
                ),
            ],
            execute=execute,
        )

    async def _build_query_vector(
        self,
        user_id: str | None,
        persona: str | None,
    ) -> tuple[list[float], set[str]]:
        if user_id:
            vector, history = await _user_vector_or_synthesize(user_id)
            return vector, {h["item_id"] for h in history}
        return await _embed_persona_text(persona or ""), set()

    async def recommend(
        self,
        *,
        user_id: str | None = None,
        persona: str | None = None,
        k: int = 10,
        dataset: str = "yelp",
        conversation_id: str | None = None,
        follow_up: str | None = None,
    ) -> dict[str, Any]:
        if not user_id and not persona:
            raise ValueError("Provide user_id or persona")

        self.reset_metrics()
        started_at = start_timer()
        already_shown = _CONVERSATIONS.shown_ids(conversation_id)
        vector, seen_items = await self._build_query_vector(user_id, persona)
        exclude_ids = list({*already_shown, *seen_items})

        pool_size = min(_CANDIDATE_CEILING, max(k * _CANDIDATE_MULTIPLIER, _CANDIDATE_FLOOR))
        candidates = await _vector_store().search(
            vector,
            k=pool_size,
            dataset=dataset,
            exclude_item_ids=exclude_ids,
        )
        candidate_json = json.dumps(
            candidates[:_RERANK_CONTEXT_ITEMS], default=str
        )[:_RERANK_CONTEXT_BUDGET_CHARS]

        prompt = self._build_user_prompt(
            dataset=dataset,
            k=k,
            candidate_json=candidate_json,
            user_id=user_id,
            persona=persona,
            follow_up=follow_up,
            exclude_ids=exclude_ids,
        )
        raw = await self.reason_and_act_json(
            (_PROMPTS / "recommend.md").read_text(encoding="utf-8"),
            prompt,
            temperature=0.3,
            max_tokens=2048,
            max_iterations=8,
            required_keys=["items", "rationale"],
        )
        payload = json.loads(raw)
        items = payload.get("items", [])[:k]

        cid = _CONVERSATIONS.remember(
            conversation_id,
            dataset,
            [it["item_id"] for it in items if it.get("item_id")],
        )
        return {
            "items": items,
            "rationale": payload.get("rationale", ""),
            "conversation_id": cid,
            "dataset": dataset,
            "meta": agent_run_meta(
                self,
                started_at,
                task="recommendation",
                mode="warm-start" if user_id else "cold-start",
                embedding_backend=_embedding_backend_label(),
                candidate_pool_size=len(candidates),
                excluded_items_count=len(exclude_ids),
                top_ann_score=(float(candidates[0]["score"]) if candidates else None),
            ),
        }

    @staticmethod
    def _build_user_prompt(
        *,
        dataset: str,
        k: int,
        candidate_json: str,
        user_id: str | None,
        persona: str | None,
        follow_up: str | None,
        exclude_ids: list[str],
    ) -> str:
        parts = [
            f"dataset={dataset}",
            f"Return top {k} recommendations as JSON.",
            f"Candidates from ANN:\n{candidate_json}",
        ]
        if user_id:
            parts.append(f"user_id={user_id}")
        if persona:
            parts.append(f"persona={persona}")
        if follow_up:
            parts.append(f"User follow-up request: {follow_up}")
        if exclude_ids:
            parts.append(f"Exclude already-seen items: {exclude_ids[:20]}")
        return "\n".join(parts)
