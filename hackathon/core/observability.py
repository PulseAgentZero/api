"""Small observability helpers for hackathon API responses."""

from __future__ import annotations

from time import perf_counter
from typing import Any

from app.agents.base import BaseAgent


def start_timer() -> float:
    return perf_counter()


def agent_run_meta(
    agent: BaseAgent,
    started_at: float,
    **extra: Any,
) -> dict[str, Any]:
    """Return a compact, response-safe view of the latest agent run.

    `BaseAgent` already tracks the hard counters. This helper only adds wall
    clock latency and the model/provider declared for the agent.
    """
    summary = agent.get_metrics_summary()
    meta: dict[str, Any] = {
        "agent": summary["agent"],
        "model": agent.default_model,
        "primary_provider": agent.provider.value,
        "providers_used": summary["providers_used"],
        "llm_calls": summary["llm_calls"],
        "tool_calls": summary["tool_calls"],
        "tool_failures": summary["tool_failures"],
        "prompt_tokens": summary["prompt_tokens"],
        "completion_tokens": summary["completion_tokens"],
        "total_tokens": summary["total_tokens"],
        "llm_duration_ms": summary["duration_ms"],
        "latency_ms": int((perf_counter() - started_at) * 1000),
        "validation_retries": summary["validation_retries"],
        "provider_fallbacks": summary["provider_fallbacks"],
    }
    meta.update({k: v for k, v in extra.items() if v is not None})
    return meta
