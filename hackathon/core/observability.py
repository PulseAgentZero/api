"""Hackathon-facing re-export of the shared agent observability helper.

The implementation now lives at :mod:`app.agents.observability` so the
production simulation routes and the hackathon containers share one source
of truth.
"""

from app.agents.observability import agent_run_meta, start_timer

__all__ = ["agent_run_meta", "start_timer"]
