"""Hackathon-only agent runtime overrides.

The promoted production agents keep their own defaults. This helper lets the
hackathon containers opt into lower-latency providers/models from environment
variables without changing production behavior.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.base import LLMProvider

from hackathon.config import HACKATHON_LLM_MODEL, HACKATHON_LLM_PROVIDER

logger = logging.getLogger(__name__)


def apply_hackathon_llm_config(agent: Any) -> None:
    """Apply optional HACKATHON_LLM_PROVIDER / HACKATHON_LLM_MODEL overrides."""
    if HACKATHON_LLM_PROVIDER:
        try:
            agent.provider = LLMProvider(HACKATHON_LLM_PROVIDER)
        except ValueError:
            logger.warning(
                "Ignoring invalid HACKATHON_LLM_PROVIDER=%r; expected anthropic or groq",
                HACKATHON_LLM_PROVIDER,
            )
    if HACKATHON_LLM_MODEL:
        agent.default_model = HACKATHON_LLM_MODEL

