"""Specialized conversational agents — Query + Synthesis split.

Implements a hierarchical pattern. A Query Agent does retrieval (read-only data tools) and produces a structured data dict; a Synthesis Agent consumes that data + the user's question and writes the final natural-language reply.

Feature-flagged via settings.CONV_AGENT_SPLIT_ENABLED. When off, agent_service.run uses the existing single-agent path. Streaming stays single-agent for now."""

from __future__ import annotations

import json
import logging
from typing import Any

from anthropic import AsyncAnthropic
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.infrastructure.database.models.user import User

logger = logging.getLogger(__name__)


# Tools the Query Agent gets — retrieval-only. The action_draft generation
# tool is handled exclusively by the Synthesis Agent (or kept on the single
# agent when the split is disabled).
QUERY_TOOLS_NAMES = (
    "get_overview",
    "get_pipeline_status",
    "get_pipeline_detail",
    "get_entities",
    "get_entity_detail",
    "get_recommendations",
    "get_outcome_analysis",
    "get_entity_trend",
    "compare_pipeline_runs",
    "find_similar_entities",
)


from app.agents.prompts.specialized_agents import (
    QUERY_AGENT_SYSTEM_SUFFIX as _QUERY_SYSTEM_SUFFIX,
    SYNTHESIS_AGENT_SYSTEM as _SYNTHESIS_SYSTEM,
)


async def query_agent_run(
    db: AsyncSession,
    current_user: User,
    conversation_messages: list[dict],
    *,
    base_system_prompt: str,
    run_tool: Any,
    full_tools: list[dict],
    json_ready: Any,
) -> dict:
    """Run the Query agent: retrieval tools only. Returns a structured data dict."""
    if not settings.is_anthropic_configured():
        return {}

    query_tools = [t for t in full_tools if t.get("name") in QUERY_TOOLS_NAMES]
    if not query_tools:
        return {}

    client = AsyncAnthropic(api_key=settings.get_anthropic_api_key())
    messages = [
        {"role": msg["role"], "content": msg["content"]}
        for msg in conversation_messages
        if msg.get("role") in {"user", "assistant"} and msg.get("content")
    ]
    system = base_system_prompt + _QUERY_SYSTEM_SUFFIX

    try:
        response = await client.messages.create(
            model=settings.ANTHROPIC_LLM_MODEL,
            max_tokens=900,
            system=system,
            tools=query_tools,
            messages=messages,
        )

        accumulated: dict[str, Any] = {}
        for _ in range(4):
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                # Final assistant message — try to parse JSON from text content.
                text = "".join(
                    b.text for b in response.content if b.type == "text"
                ).strip()
                if text:
                    parsed = _try_parse_json(text)
                    if isinstance(parsed, dict):
                        accumulated.update(parsed)
                return accumulated

            messages.append(
                {"role": "assistant", "content": [b.model_dump() for b in response.content]}
            )
            tool_results = []
            for tool_use in tool_uses:
                result = await run_tool(
                    tool_use.name,
                    dict(tool_use.input or {}),
                    db,
                    current_user.org_id,
                )
                accumulated[tool_use.name] = json_ready(result)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": json.dumps(json_ready(result), ensure_ascii=False),
                    }
                )
            messages.append({"role": "user", "content": tool_results})
            response = await client.messages.create(
                model=settings.ANTHROPIC_LLM_MODEL,
                max_tokens=900,
                system=system,
                tools=query_tools,
                messages=messages,
            )
        return accumulated
    except Exception as exc:
        logger.warning("[query_agent] failed: %s", exc)
        return {}


async def synthesis_agent_run(
    db: AsyncSession,
    current_user: User,
    conversation_messages: list[dict],
    retrieved_data: dict,
    *,
    base_system_prompt: str,
    extra_instruction: str = "",
) -> str:
    """Run the Synthesis agent: produce a natural-language reply from retrieved data."""
    if not settings.is_anthropic_configured():
        return ""

    user_question = ""
    for msg in reversed(conversation_messages):
        if msg.get("role") == "user" and msg.get("content"):
            content = msg["content"]
            if isinstance(content, str):
                user_question = content
                break

    if not user_question:
        return ""

    payload_json = json.dumps(retrieved_data, default=str)[:6000]
    user_msg = (
        f"User question: {user_question}\n\n"
        f"Data gathered by the Query agent:\n{payload_json}\n\n"
        "Write a natural, conversational reply now as Pulse AI, an intelligent copilot. "
        "Warm and helpful, grounded only in the data above. Never use em dashes (—) or "
        "en dashes (–); use commas, colons, or periods instead."
    )
    if extra_instruction.strip():
        user_msg += f"\n\nAdditional instruction: {extra_instruction.strip()}"
    system = base_system_prompt + "\n" + _SYNTHESIS_SYSTEM

    try:
        client = AsyncAnthropic(api_key=settings.get_anthropic_api_key())
        response = await client.messages.create(
            model=settings.ANTHROPIC_LLM_MODEL,
            max_tokens=900,
            temperature=0.75,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        return text
    except Exception as exc:
        logger.warning("[synthesis_agent] failed: %s", exc)
        return ""


async def run_split(
    db: AsyncSession,
    current_user: User,
    conversation_messages: list[dict],
    *,
    base_system_prompt: str,
    run_tool: Any,
    full_tools: list[dict],
    json_ready: Any,
) -> str:
    """Two-phase orchestration: query → synthesis. Empty string on degraded paths."""
    retrieved = await query_agent_run(
        db, current_user, conversation_messages,
        base_system_prompt=base_system_prompt,
        run_tool=run_tool, full_tools=full_tools, json_ready=json_ready,
    )
    return await synthesis_agent_run(
        db, current_user, conversation_messages, retrieved,
        base_system_prompt=base_system_prompt,
    )


def _try_parse_json(text: str) -> Any:
    """Best-effort JSON parse; tolerates code fences."""
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if "{" in raw:
            raw = raw[raw.find("{") :]
    try:
        return json.loads(raw)
    except Exception:
        return None
