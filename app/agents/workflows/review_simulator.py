"""Review simulation agent.

Given a user persona and a product, predict an authentic star rating (1-5) and
a short review. Runs on Entivia's :class:`BaseAgent` (ReAct loop, JSON
validation, Anthropic primary with Groq fallback).

Two input modes:
- **Direct** — caller supplies ``persona`` and ``product`` dicts; the agent
  inlines them in the prompt and emits JSON without calling tools.
- **DB** — caller supplies ``user_id`` and ``item_id``; the agent fetches a
  user profile and an item via tools registered by the caller. DB tools are
  *opt-in* via ``register_db_tools=True`` so this class has no hard dependency
  on the hackathon Postgres schema.

The public route at ``/api/public/v1/simulation/review`` exposes direct mode
only. The hackathon containers (``hackathon/agents/review_simulator.py``)
import this class and pass ``register_db_tools=True`` so DB mode keeps
working against the hackathon Yelp slice.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from app.agents.base import BaseAgent, LLMProvider
from app.agents.observability import agent_run_meta, start_timer
from app.agents.tools.base import Tool, ToolParam

logger = logging.getLogger(__name__)
_PROMPTS = Path(__file__).resolve().parent / "prompts" / "simulation"

_MAX_AGENT_ITERATIONS = 6
_MAX_OUTPUT_TOKENS = 1024
_RETRY_ATTEMPTS = 2

_RATING_DIRECT_MAX_TOKENS = 96
_RATING_DB_MAX_TOKENS = 384


class ReviewParseError(ValueError):
    """Raised when the LLM JSON cannot be coerced into (stars, text)."""


class ReviewSimulationAgent(BaseAgent):
    def __init__(self, *, register_db_tools: bool = False) -> None:
        super().__init__(
            name="review_simulator",
            provider=LLMProvider.ANTHROPIC,
            fallback_enabled=True,
        )
        if register_db_tools:
            self._register_db_tools()

    def _register_db_tools(self) -> None:
        """Register tools that read from the hackathon's Yelp Postgres slice.

        Imported lazily to keep ``app/`` free of any hard dependency on
        ``hackathon/``. Production callers that only need direct mode never
        execute this path.
        """
        from hackathon.core.repository import fetch_item, fetch_user_profile

        async def fetch_profile(user_id: str) -> dict[str, Any]:
            data = await fetch_user_profile(user_id)
            return data or {"error": f"Unknown user_id: {user_id}"}

        async def fetch_item_tool(item_id: str) -> dict[str, Any]:
            data = await fetch_item(item_id)
            return data or {"error": f"Unknown item_id: {item_id}"}

        self.registry.register(
            Tool(
                name="fetch_user_profile",
                description="Load user persona: avg stars, categories, sample past reviews.",
                parameters=[ToolParam("user_id", "string", "Yelp user id", required=True)],
                execute=fetch_profile,
            )
        )
        self.registry.register(
            Tool(
                name="fetch_item",
                description="Load target item/business metadata.",
                parameters=[ToolParam("item_id", "string", "Business/item id", required=True)],
                execute=fetch_item_tool,
            )
        )

    def _system_prompt(self, voice: str) -> str:
        prompt_file = "review_nigerian.md" if voice == "nigerian" else "review_default.md"
        return (_PROMPTS / prompt_file).read_text(encoding="utf-8")

    async def simulate(
        self,
        *,
        user_id: str | None = None,
        item_id: str | None = None,
        persona: dict[str, Any] | None = None,
        product: dict[str, Any] | None = None,
        voice: str = "default",
    ) -> dict[str, Any]:
        """Simulate a review.

        - **Direct** — pass ``persona`` + ``product`` dicts (no DB lookup).
        - **DB** — pass ``user_id`` + ``item_id``; requires the agent to have
          been constructed with ``register_db_tools=True``.
        """
        if persona is not None and product is not None:
            input_mode = "direct"
        elif user_id and item_id:
            input_mode = "db"
        else:
            raise ValueError(
                "Provide (persona + product) for direct mode, or (user_id + item_id) for DB mode."
            )

        self.reset_metrics()
        started_at = start_timer()
        try:
            stars, text = await self._simulate_once(
                user_id=user_id,
                item_id=item_id,
                persona=persona,
                product=product,
                voice=voice,
                input_mode=input_mode,
            )
        except ReviewParseError as exc:
            raise RuntimeError(f"Review simulation failed: {exc}") from exc

        return {
            "user_id": user_id,
            "item_id": item_id,
            "stars": stars,
            "text": text,
            "meta": agent_run_meta(
                self,
                started_at,
                task="review_simulation",
                voice=voice,
                input_mode=input_mode,
            ),
        }

    async def simulate_from_context(
        self,
        *,
        user_id: str,
        item_id: str,
        persona: dict[str, Any],
        product: dict[str, Any],
        voice: str = "default",
    ) -> dict[str, Any]:
        """Simulate DB-mode output using server-prefetched context."""
        self.reset_metrics()
        started_at = start_timer()
        try:
            stars, text = await self._simulate_once(
                user_id=user_id,
                item_id=item_id,
                persona=persona,
                product=product,
                voice=voice,
                input_mode="direct",
            )
        except ReviewParseError as exc:
            raise RuntimeError(f"Review simulation failed: {exc}") from exc

        return {
            "user_id": user_id,
            "item_id": item_id,
            "stars": stars,
            "text": text,
            "meta": agent_run_meta(
                self,
                started_at,
                task="review_simulation",
                voice=voice,
                input_mode="db_prefetched",
            ),
        }

    async def predict_rating(
        self,
        *,
        user_id: str | None = None,
        item_id: str | None = None,
        persona: dict[str, Any] | None = None,
        product: dict[str, Any] | None = None,
        input_mode: str | None = None,
    ) -> dict[str, Any]:
        """Predict stars only with a smaller prompt and token budget.

        Token budgets are per ReAct iteration, not per request. Direct mode
        emits only a tiny JSON object, but DB mode has to fit ``tool_use``
        blocks (50-150 tokens each on Anthropic) plus the final JSON in a
        single iteration, so it needs a larger ceiling to avoid mid-response
        truncation.
        """
        if persona is not None and product is not None:
            resolved_mode = input_mode or "direct"
            user_prompt = self._rating_prompt_from_context(persona, product)
            max_iterations = 1
            max_tokens = _RATING_DIRECT_MAX_TOKENS
        elif user_id and item_id:
            resolved_mode = input_mode or "db"
            user_prompt = (
                f"Predict only the star rating for user_id={user_id} on item_id={item_id}. "
                "Call fetch_user_profile and fetch_item first, then return JSON: "
                '{"stars": <1-5>}. Do not write a review.'
            )
            max_iterations = _MAX_AGENT_ITERATIONS
            max_tokens = _RATING_DB_MAX_TOKENS
        else:
            raise ValueError(
                "Provide (persona + product) for direct mode, or (user_id + item_id) for DB mode."
            )

        self.reset_metrics()
        started_at = start_timer()
        try:
            raw = await self.reason_and_act_json(
                self._rating_system_prompt(),
                user_prompt,
                temperature=0.1,
                max_tokens=max_tokens,
                max_iterations=max_iterations,
                required_keys=["stars"],
            )
            stars = self._parse_stars(raw)
        except ReviewParseError as exc:
            raise RuntimeError(f"Rating prediction failed: {exc}") from exc
        return {
            "user_id": user_id,
            "item_id": item_id,
            "stars": stars,
            "meta": agent_run_meta(
                self,
                started_at,
                task="rating_prediction",
                input_mode=resolved_mode,
            ),
        }

    @retry(
        retry=retry_if_exception_type(ReviewParseError),
        stop=stop_after_attempt(_RETRY_ATTEMPTS),
        wait=wait_fixed(0),
        reraise=True,
    )
    async def _simulate_once(
        self,
        *,
        user_id: str | None,
        item_id: str | None,
        persona: dict[str, Any] | None,
        product: dict[str, Any] | None,
        voice: str,
        input_mode: str,
    ) -> tuple[int, str]:
        if input_mode == "direct":
            user_prompt = self._direct_prompt(persona or {}, product or {})
            max_iterations = 1
        else:
            user_prompt = (
                f"Simulate a review for user_id={user_id} on unseen item_id={item_id}. "
                "Call fetch_user_profile and fetch_item first, then return JSON with stars and text."
            )
            max_iterations = _MAX_AGENT_ITERATIONS

        raw = await self.reason_and_act_json(
            self._system_prompt(voice),
            user_prompt,
            temperature=0.4,
            max_tokens=_MAX_OUTPUT_TOKENS,
            max_iterations=max_iterations,
            required_keys=["stars", "text"],
        )
        return self._parse_response(raw)

    @staticmethod
    def _direct_prompt(persona: dict[str, Any], product: dict[str, Any]) -> str:
        parts = [
            "Simulate a review using the persona and product below. Do NOT call any tools.",
            f"\n## User persona\n{json.dumps(persona, indent=2, default=str)}",
            f"\n## Product / item\n{json.dumps(product, indent=2, default=str)}",
            "\nReturn JSON: {\"stars\": <1-5>, \"text\": \"<review>\"}",
        ]
        return "".join(parts)

    @staticmethod
    def _rating_system_prompt() -> str:
        return (
            "You predict how a specific user persona would rate a product. "
            'Return strict JSON only with this shape: {"stars": <integer 1-5>}. '
            "Do not include prose, markdown, or a review body."
        )

    @staticmethod
    def _rating_prompt_from_context(persona: dict[str, Any], product: dict[str, Any]) -> str:
        return (
            "Predict only the star rating for this persona and product.\n"
            f"\n## User persona\n{json.dumps(persona, indent=2, default=str)}"
            f"\n## Product / item\n{json.dumps(product, indent=2, default=str)}"
            '\nReturn JSON: {"stars": <1-5>}'
        )

    @staticmethod
    def _parse_stars(raw: str) -> int:
        try:
            data = json.loads(raw)
            return max(1, min(5, int(data["stars"])))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("rating JSON parse failed: %s", exc)
            raise ReviewParseError(str(exc)) from exc

    @staticmethod
    def _parse_response(raw: str) -> tuple[int, str]:
        try:
            data = json.loads(raw)
            stars = max(1, min(5, int(data["stars"])))
            text = str(data["text"]).strip()
            if not text:
                raise ReviewParseError("empty review text")
            return stars, text
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("review JSON parse failed: %s", exc)
            raise ReviewParseError(str(exc)) from exc
