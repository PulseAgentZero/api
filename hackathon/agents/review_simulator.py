"""Task A agent — predict (stars, text) for a user × item pair."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from app.agents.base import BaseAgent, LLMProvider
from app.agents.tools.base import Tool, ToolParam

from hackathon.core.observability import agent_run_meta, start_timer
from hackathon.core.repository import fetch_item, fetch_user_profile

logger = logging.getLogger(__name__)
_PROMPTS = Path(__file__).resolve().parent / "prompts"

_MAX_AGENT_ITERATIONS = 6
_MAX_OUTPUT_TOKENS = 1024
_RETRY_ATTEMPTS = 2


class ReviewParseError(ValueError):
    """Raised when the LLM JSON cannot be coerced into (stars, text)."""


class ReviewSimulationAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(
            name="review_simulator",
            provider=LLMProvider.ANTHROPIC,
            fallback_enabled=True,
        )
        self._register_tools()

    def _register_tools(self) -> None:
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

        Two input modes (challenge-compliant):
        - **Direct** — pass ``persona`` + ``product`` dicts (no DB lookup).
        - **DB** — pass ``user_id`` + ``item_id``; agent fetches profile and item via tools.
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
            max_iterations = 1  # no tools needed
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
