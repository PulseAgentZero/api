"""Hackathon-facing shim for the promoted Cold-Start Recommendation Agent.

The implementation now lives at
:mod:`app.agents.workflows.cold_start_recommender`. Re-exported here under
its hackathon name (``RecommendationAgent``) so the existing hackathon
container (``task-b-api``) and the eval harness keep working unchanged.
"""

from app.agents.workflows.cold_start_recommender import (
    ColdStartRecommendationAgent as RecommendationAgent,
)

__all__ = ["RecommendationAgent"]
