"""LLM-as-judge for narrative quality.

Scores risk narratives and recommendations against the entity payload that
generated them. Used by the eval harness to detect regressions in:

- Whether the LLM grounds output in specific signal values
- Whether it actually uses retrieved `similar_entities` / `past_recommendations`
- Whether the suggested action is concrete and doable

Judge is Claude Sonnet 4.6 (independent of the Groq narrator/recommender, so
the score is a real cross-check rather than self-grading).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from anthropic import AsyncAnthropic

from app.config.settings import settings

logger = logging.getLogger(__name__)

_JUDGE_SYSTEM = """You are a strict evaluator of risk-narrative quality.

Given an entity payload and the narrative/recommendation generated about it,
score the output on three axes from 0.0 (absent) to 1.0 (excellent):

1. uses_signal_values: Does the text reference specific signal values from the
   entity's `signal_values` / `behavioural_metrics`? Numbers, not vague words.
2. references_similar: If the entity payload includes a non-empty
   `similar_entities` array, did the text actually use that precedent (citing a
   similar entity_id or its pattern)? If `similar_entities` is empty/missing,
   score 1.0 (vacuously satisfied).
3. actionable: Is the output concrete enough for an ops manager to act on
   immediately? Generic advice scores low.

Return ONLY a JSON object of the form:
{
  "uses_signal_values": 0.0,
  "references_similar": 0.0,
  "actionable": 0.0,
  "notes": "one short sentence on what was strong/weak"
}
No prose, no markdown fences."""


@dataclass
class JudgeScore:
    uses_signal_values: float
    references_similar: float
    actionable: float
    notes: str = ""

    @property
    def overall(self) -> float:
        return (self.uses_signal_values + self.references_similar + self.actionable) / 3


class NarrativeJudge:
    """Async judge that scores a narrative against its source entity payload."""

    def __init__(self, model: str | None = None) -> None:
        self.model = model or settings.ANTHROPIC_LLM_MODEL
        # self.model = model or "claude-sonnet-4-5-20250929"
        self._client: AsyncAnthropic | None = None

    def _get_client(self) -> AsyncAnthropic | None:
        if not settings.is_anthropic_configured():
            return None
        if self._client is None:
            self._client = AsyncAnthropic(api_key=settings.get_anthropic_api_key())
        return self._client

    async def score(
        self,
        *,
        entity: dict[str, Any],
        narrative: str,
    ) -> JudgeScore | None:
        """Score one narrative. Returns None if the judge is unavailable."""
        client = self._get_client()
        if client is None:
            logger.debug("[NarrativeJudge] ANTHROPIC_API_KEY not set; skipping")
            return None

        user_msg = (
            f"Entity payload:\n{json.dumps(entity, default=str)[:4000]}\n\n"
            f"Generated text:\n{narrative}"
        )
        try:
            response = await client.messages.create(
                model=self.model,
                max_tokens=300,
                system=_JUDGE_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
                temperature=0.0,
            )
            text = "".join(b.text for b in response.content if b.type == "text").strip()
            # The judge is instructed to return raw JSON; be lenient against fences.
            if text.startswith("```"):
                text = text.strip("`")
                text = text.split("\n", 1)[1] if "\n" in text else text
                text = text.rsplit("```", 1)[0]
            data = json.loads(text)
            return JudgeScore(
                uses_signal_values=float(data.get("uses_signal_values", 0.0)),
                references_similar=float(data.get("references_similar", 0.0)),
                actionable=float(data.get("actionable", 0.0)),
                notes=str(data.get("notes", "")),
            )
        except Exception as exc:
            logger.warning("[NarrativeJudge] scoring failed: %s", exc)
            return None

    async def score_batch(
        self, items: list[tuple[dict[str, Any], str]]
    ) -> list[JudgeScore | None]:
        """Score multiple (entity, narrative) pairs sequentially."""
        return [await self.score(entity=e, narrative=n) for e, n in items]


narrative_judge = NarrativeJudge()
