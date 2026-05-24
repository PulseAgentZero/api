"""Hackathon-facing shim for the promoted Review Simulation Agent.

The implementation now lives at
:mod:`app.agents.workflows.review_simulator`. The hackathon container
constructs the agent with ``register_db_tools=True`` so DB-mode (user_id +
item_id against the Yelp slice) still works.
"""

from __future__ import annotations

from app.agents.workflows.review_simulator import (
    ReviewParseError,
    ReviewSimulationAgent as _PromotedReviewSimulationAgent,
)
from hackathon.agents.runtime import apply_hackathon_llm_config


class ReviewSimulationAgent(_PromotedReviewSimulationAgent):
    """Hackathon entry point — wires DB-mode tools onto the production agent."""

    def __init__(self) -> None:
        super().__init__(register_db_tools=True)
        apply_hackathon_llm_config(self)


__all__ = ["ReviewSimulationAgent", "ReviewParseError"]
