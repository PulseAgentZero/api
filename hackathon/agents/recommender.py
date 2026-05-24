"""Hackathon-facing shim for the promoted Cold-Start Recommendation Agent.

The implementation now lives at
:mod:`app.agents.workflows.cold_start_recommender`. Re-exported here under
its hackathon name (``RecommendationAgent``) so the existing hackathon
container (``task-b-api``) and the eval harness keep working unchanged.
"""

from app.agents.workflows.cold_start_recommender import ColdStartRecommendationAgent

from hackathon.agents.runtime import apply_hackathon_llm_config


class RecommendationAgent(ColdStartRecommendationAgent):
    """Hackathon recommender with optional env-driven fast model override."""

    def __init__(self) -> None:
        super().__init__()
        apply_hackathon_llm_config(self)

__all__ = ["RecommendationAgent"]
