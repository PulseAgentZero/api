"""Base agent with dual-provider LLM support (Anthropic Claude + Groq).

Production-grade agent infrastructure supporting:
- Anthropic Claude: native tool_use API, structured content blocks
- Groq (OpenAI-compatible): function calling via chat.completions
- Automatic provider fallback (Groq fails → tries Claude, and vice versa)
- ReAct loop with automatic tool execution and result truncation
- JSON output validation with structured retry
- Per-step reasoning capture for full observability
- Configurable model selection per agent
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import anthropic
from groq import AsyncGroq

from app.agents.tools.base import ToolCall, ToolRegistry, ToolResult, parse_tool_calls_from_claude
from app.config.settings import settings

logger = logging.getLogger(__name__)

MAX_REACT_ITERATIONS = 12
MAX_VALIDATION_RETRIES = 3
MAX_TOOL_RESULT_CHARS = 12_000  # Truncate tool output to prevent context blowup
_COMPRESS_TAIL_CHARS = 300     # Chars kept per old tool message after compression


class LLMProvider(str, Enum):
    ANTHROPIC = "anthropic"
    GROQ = "groq"

_FALLBACK_CONFIG = {
    LLMProvider.GROQ: (LLMProvider.ANTHROPIC, settings.ANTHROPIC_LLM_MODEL),
    LLMProvider.ANTHROPIC: (LLMProvider.GROQ, settings.GROQ_LLM_MODEL_HEAVY),
}


@dataclass
class AgentMetrics:
    """Tracks cumulative metrics for a single agent run."""
    total_llm_calls: int = 0
    total_tool_calls: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_duration_ms: int = 0
    tool_failures: int = 0
    validation_retries: int = 0
    provider_fallbacks: int = 0
    compression_count: int = 0
    providers_used: list[str] = field(default_factory=list)


class JsonValidator:
    """Validates agent output is parseable JSON with expected keys."""

    def __init__(self, required_keys: list[str] | None = None) -> None:
        self.required_keys = required_keys or []

    def validate(self, output: str) -> tuple[bool, str]:
        stripped = _strip_fences(output)
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as e:
            return False, f"Invalid JSON: {e}"
        if not isinstance(data, dict):
            return False, f"Expected JSON object, got {type(data).__name__}"
        missing = [k for k in self.required_keys if k not in data]
        if missing:
            return False, f"Missing required keys: {missing}"
        return True, ""


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        m = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", stripped, re.DOTALL)
        if m:
            return m.group(1).strip()
    return stripped


def _truncate_tool_output(data: Any, max_chars: int = MAX_TOOL_RESULT_CHARS) -> str:
    """Serialize and truncate tool output to prevent LLM context overflow."""
    raw = json.dumps(data, default=str)
    if len(raw) <= max_chars:
        return raw
    truncated = raw[:max_chars]
    return truncated + f'\n... [TRUNCATED: {len(raw)} chars total, showing first {max_chars}]'


def _estimate_context_chars(messages: list[dict[str, Any]]) -> int:
    """Rough character count of a message list (proxy for token usage)."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    total += len(str(item.get("content", "")))
                    total += len(str(item.get("text", "")))
    return total


def _compress_old_tool_outputs(
    messages: list[dict[str, Any]],
    *,
    keep_recent: int,
    tail_chars: int = _COMPRESS_TAIL_CHARS,
) -> int:
    """Aggressively truncate tool outputs in older messages to free context space.

    Leaves the `keep_recent` most-recent messages untouched. Returns total chars
    removed. Mutates `messages` in-place — no LLM call needed.
    """
    if len(messages) <= keep_recent + 2:
        return 0

    compress_targets = messages[: max(0, len(messages) - keep_recent)]
    removed = 0

    for msg in compress_targets:
        content = msg.get("content")
        if isinstance(content, str) and len(content) > tail_chars:
            removed += len(content) - tail_chars
            msg["content"] = content[:tail_chars] + " ...[compressed]"
        elif isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                # Anthropic tool_result blocks
                if item.get("type") == "tool_result":
                    c = item.get("content", "")
                    if isinstance(c, str) and len(c) > tail_chars:
                        removed += len(c) - tail_chars
                        item["content"] = c[:tail_chars] + " ...[compressed]"
                # Groq function call arguments
                fn = item.get("function", {})
                if fn and isinstance(fn.get("arguments", ""), str):
                    a = fn["arguments"]
                    if len(a) > tail_chars:
                        removed += len(a) - tail_chars
                        fn["arguments"] = a[:tail_chars] + " ...[compressed]"

    return removed


class BaseAgent:
    """Base class for Pulse autonomous agents.

    Supports both Anthropic Claude and Groq as LLM providers. Each agent
    declares its preferred provider and model at init time. Includes
    automatic fallback if the primary provider fails.
    """

    def __init__(
        self,
        name: str,
        provider: LLMProvider = LLMProvider.ANTHROPIC,
        default_model: str | None = None,
        fallback_enabled: bool = True,
    ) -> None:
        self.name = name
        self.provider = provider
        self.fallback_enabled = fallback_enabled
        self._reasoning_entries: list[dict] = []
        self._metrics = AgentMetrics()
        self.registry = ToolRegistry()

        if default_model:
            self.default_model = default_model
        elif provider == LLMProvider.GROQ:
            self.default_model = settings.GROQ_LLM_MODEL
        else:
            self.default_model = settings.ANTHROPIC_LLM_MODEL

        self._anthropic_client: Optional[anthropic.AsyncAnthropic] = None
        self._groq_client: Optional[AsyncGroq] = None

    @property
    def anthropic_client(self) -> anthropic.AsyncAnthropic:
        if self._anthropic_client is None:
            api_key = settings.get_anthropic_api_key()
            if not api_key:
                raise ValueError("ANTHROPIC_API_KEY not configured")
            self._anthropic_client = anthropic.AsyncAnthropic(
                api_key=api_key, max_retries=3, timeout=180.0,
            )
        return self._anthropic_client

    @property
    def groq_client(self) -> AsyncGroq:
        if self._groq_client is None:
            api_key = settings.groq_api_key
            if not api_key:
                raise ValueError("GROQ_API_KEY not configured")
            self._groq_client = AsyncGroq(
                api_key=api_key, max_retries=3, timeout=120.0,
            )
        return self._groq_client

    def reset_metrics(self) -> None:
        self._reasoning_entries = []
        self._metrics = AgentMetrics()

    # ─── ReAct Loop (with provider fallback) ───────────────────────────

    async def reason_and_act(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        provider: LLMProvider | None = None,
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        max_iterations: int = MAX_REACT_ITERATIONS,
        output_validator: Optional[JsonValidator] = None,
    ) -> str:
        """ReAct loop with automatic provider fallback on failure."""
        provider = provider or self.provider
        model = model or self.default_model

        try:
            return await self._react_dispatch(
                system_prompt, user_prompt,
                provider=provider, model=model, temperature=temperature,
                max_tokens=max_tokens, max_iterations=max_iterations,
                output_validator=output_validator,
            )
        except Exception as primary_err:
            if not self.fallback_enabled:
                raise

            fb_provider, fb_model = _FALLBACK_CONFIG.get(provider, (None, None))
            if fb_provider is None:
                raise

            # Check if the fallback provider is actually configured
            try:
                if fb_provider == LLMProvider.ANTHROPIC:
                    _ = self.anthropic_client
                else:
                    _ = self.groq_client
            except ValueError:
                logger.warning("[%s] Fallback provider %s not configured", self.name, fb_provider.value)
                raise primary_err

            self._metrics.provider_fallbacks += 1
            logger.warning(
                "[%s] Primary provider %s failed (%s), falling back to %s/%s",
                self.name, provider.value, primary_err, fb_provider.value, fb_model,
            )
            return await self._react_dispatch(
                system_prompt, user_prompt,
                provider=fb_provider, model=fb_model, temperature=temperature,
                max_tokens=max_tokens, max_iterations=max_iterations,
                output_validator=output_validator,
            )

    async def _react_dispatch(self, system_prompt, user_prompt, *, provider, **kwargs) -> str:
        if provider == LLMProvider.ANTHROPIC:
            return await self._react_anthropic(system_prompt, user_prompt, **kwargs)
        return await self._react_groq(system_prompt, user_prompt, **kwargs)

    async def reason_and_act_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        provider: LLMProvider | None = None,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        max_iterations: int = MAX_REACT_ITERATIONS,
        required_keys: list[str] | None = None,
    ) -> str:
        """ReAct loop that validates output is well-formed JSON."""
        json_suffix = (
            "\n\nIMPORTANT: Your final answer MUST be a valid JSON object. "
            "No markdown fences, no explanation outside the JSON — just the raw JSON object."
        )
        validator = JsonValidator(required_keys=required_keys)
        raw = await self.reason_and_act(
            system_prompt=system_prompt,
            user_prompt=user_prompt + json_suffix,
            provider=provider, model=model, temperature=temperature,
            max_tokens=max_tokens, max_iterations=max_iterations,
            output_validator=validator,
        )
        return _extract_json(raw)

    # ─── Single-shot LLM calls (with fallback) ─────────────────────────

    async def llm_call(
        self, system_prompt: str, user_prompt: str,
        *, provider: LLMProvider | None = None,
        model: str | None = None,
        temperature: float = 0.1, max_tokens: int = 4096,
    ) -> str:
        provider = provider or self.provider
        model = model or self.default_model
        try:
            return await self._single_dispatch(
                system_prompt, user_prompt,
                provider=provider, model=model,
                temperature=temperature, max_tokens=max_tokens,
            )
        except Exception as primary_err:
            if not self.fallback_enabled:
                raise
            fb_provider, fb_model = _FALLBACK_CONFIG.get(provider, (None, None))
            if fb_provider is None:
                raise
            try:
                if fb_provider == LLMProvider.ANTHROPIC:
                    _ = self.anthropic_client
                else:
                    _ = self.groq_client
            except ValueError:
                raise primary_err
            self._metrics.provider_fallbacks += 1
            logger.warning("[%s] Fallback to %s for single-shot call", self.name, fb_provider.value)
            return await self._single_dispatch(
                system_prompt, user_prompt,
                provider=fb_provider, model=fb_model,
                temperature=temperature, max_tokens=max_tokens,
            )

    async def _single_dispatch(self, system_prompt, user_prompt, *, provider, **kwargs) -> str:
        if provider == LLMProvider.ANTHROPIC:
            return await self._call_anthropic(system_prompt, user_prompt, **kwargs)
        return await self._call_groq(system_prompt, user_prompt, **kwargs)

    async def llm_json_call(
        self, system_prompt: str, user_prompt: str,
        *, provider: LLMProvider | None = None,
        model: str | None = None, temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        raw = await self.llm_call(
            system_prompt, user_prompt + "\n\nRespond with ONLY valid JSON.",
            provider=provider, model=model, temperature=temperature,
            max_tokens=max_tokens,
        )
        return _extract_json(raw)

    # ─── Anthropic Claude Backend ──────────────────────────────────────

    async def _react_anthropic(
        self, system_prompt: str, user_prompt: str, *,
        model: str, temperature: float, max_tokens: int,
        max_iterations: int, output_validator: Optional[JsonValidator],
    ) -> str:
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]
        tools_schema = self.registry.to_claude_tools() if self.registry.tools else None
        iteration = 0
        validation_retries = 0

        logger.info("[%s] ReAct start (anthropic): model=%s, tools=%d", self.name, model, len(self.registry.tools))

        compress_threshold = settings.AGENT_CONTEXT_COMPRESS_THRESHOLD
        keep_recent = settings.AGENT_CONTEXT_COMPRESS_KEEP_RECENT

        while iteration < max_iterations:
            iteration += 1

            # Context compression: when accumulated tool outputs grow large, truncate
            # old messages so the context window stays manageable.
            if compress_threshold > 0 and _estimate_context_chars(messages) > compress_threshold:
                removed = _compress_old_tool_outputs(messages, keep_recent=keep_recent)
                if removed > 0:
                    self._metrics.compression_count += 1
                    logger.info(
                        "[%s] Context compressed at iter %d: removed ~%d chars",
                        self.name, iteration, removed,
                    )

            kwargs: dict[str, Any] = dict(
                model=model, max_tokens=max_tokens,
                system=system_prompt, messages=messages, temperature=temperature,
            )
            if tools_schema:
                kwargs["tools"] = tools_schema

            t0 = time.monotonic()
            response = await self.anthropic_client.messages.create(**kwargs)
            elapsed = int((time.monotonic() - t0) * 1000)

            usage = response.usage
            self._metrics.total_llm_calls += 1
            self._metrics.total_prompt_tokens += usage.input_tokens if usage else 0
            self._metrics.total_completion_tokens += usage.output_tokens if usage else 0
            self._metrics.total_duration_ms += elapsed
            if "anthropic" not in self._metrics.providers_used:
                self._metrics.providers_used.append("anthropic")

            self._record_reasoning(
                thinking=_first_text_anthropic(response.content)[:500],
                model=model, provider="anthropic",
                prompt_tokens=usage.input_tokens if usage else 0,
                completion_tokens=usage.output_tokens if usage else 0,
                duration_ms=elapsed, iteration=iteration,
            )

            tool_calls = parse_tool_calls_from_claude(response.content)

            if not tool_calls:
                final = _first_text_anthropic(response.content)
                if output_validator and validation_retries < MAX_VALIDATION_RETRIES:
                    is_valid, err = output_validator.validate(final)
                    if not is_valid:
                        validation_retries += 1
                        self._metrics.validation_retries += 1
                        logger.warning("[%s] Validation failed (retry %d): %s", self.name, validation_retries, err)
                        messages.append({"role": "assistant", "content": response.content})
                        if not final.strip():
                            retry_msg = "Your response was empty. You MUST output a complete JSON object with all required keys. Return ONLY the JSON object now."
                        else:
                            retry_msg = f"INVALID output: {err}\nFix it. Return ONLY corrected JSON."
                        messages.append({"role": "user", "content": retry_msg})
                        continue
                logger.info("[%s] Anthropic ReAct done: %d iters", self.name, iteration)
                return final

            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for tc in tool_calls:
                result = await self.registry.execute(tc)
                self._metrics.total_tool_calls += 1
                if not result.success:
                    self._metrics.tool_failures += 1
                logger.info("[%s] Tool %s → %s (%dms)", self.name, tc.tool_name, "OK" if result.success else "FAIL", result.duration_ms)
                content = _truncate_tool_output(result.data) if result.success else json.dumps({"error": result.error})
                tool_results.append({"type": "tool_result", "tool_use_id": tc.call_id, "content": content})
            messages.append({"role": "user", "content": tool_results})

        logger.warning("[%s] Hit max iterations (%d)", self.name, max_iterations)
        return '{"error": "Agent reached maximum reasoning iterations"}'

    async def _call_anthropic(self, system_prompt, user_prompt, *, model, temperature, max_tokens) -> str:
        t0 = time.monotonic()
        response = await self.anthropic_client.messages.create(
            model=model, max_tokens=max_tokens, system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}], temperature=temperature,
        )
        elapsed = int((time.monotonic() - t0) * 1000)
        content = _first_text_anthropic(response.content)
        usage = response.usage
        self._metrics.total_llm_calls += 1
        self._metrics.total_prompt_tokens += usage.input_tokens if usage else 0
        self._metrics.total_completion_tokens += usage.output_tokens if usage else 0
        self._record_reasoning(thinking=content[:500], model=model, provider="anthropic",
                               prompt_tokens=usage.input_tokens if usage else 0,
                               completion_tokens=usage.output_tokens if usage else 0, duration_ms=elapsed)
        return content

    # ─── Groq Backend (OpenAI-compatible) ──────────────────────────────

    async def _react_groq(
        self, system_prompt: str, user_prompt: str, *,
        model: str, temperature: float, max_tokens: int,
        max_iterations: int, output_validator: Optional[JsonValidator],
    ) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        tools_schema = self._registry_to_openai_tools() if self.registry.tools else None
        iteration = 0
        validation_retries = 0

        logger.info("[%s] ReAct start (groq): model=%s, tools=%d", self.name, model, len(self.registry.tools))

        compress_threshold = settings.AGENT_CONTEXT_COMPRESS_THRESHOLD
        keep_recent = settings.AGENT_CONTEXT_COMPRESS_KEEP_RECENT

        while iteration < max_iterations:
            iteration += 1

            if compress_threshold > 0 and _estimate_context_chars(messages) > compress_threshold:
                removed = _compress_old_tool_outputs(messages, keep_recent=keep_recent)
                if removed > 0:
                    self._metrics.compression_count += 1
                    logger.info(
                        "[%s] Context compressed at iter %d: removed ~%d chars",
                        self.name, iteration, removed,
                    )

            kwargs: dict[str, Any] = dict(
                model=model, max_tokens=max_tokens,
                messages=messages, temperature=temperature,
            )
            if tools_schema:
                kwargs["tools"] = tools_schema
                kwargs["tool_choice"] = "auto"

            t0 = time.monotonic()
            try:
                response = await self.groq_client.chat.completions.create(**kwargs)
            except AttributeError as e:
                raise RuntimeError(f"Groq SDK response parsing failed (malformed API response): {e}") from e
            elapsed = int((time.monotonic() - t0) * 1000)

            choice = response.choices[0]
            usage = response.usage
            self._metrics.total_llm_calls += 1
            self._metrics.total_prompt_tokens += usage.prompt_tokens if usage else 0
            self._metrics.total_completion_tokens += usage.completion_tokens if usage else 0
            self._metrics.total_duration_ms += elapsed
            if "groq" not in self._metrics.providers_used:
                self._metrics.providers_used.append("groq")

            self._record_reasoning(
                thinking=(choice.message.content or "")[:500],
                model=model, provider="groq",
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
                duration_ms=elapsed, iteration=iteration,
            )

            if choice.message.tool_calls:
                messages.append({
                    "role": "assistant", "content": choice.message.content,
                    "tool_calls": [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in choice.message.tool_calls
                    ],
                })
                for tc in choice.message.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        logger.warning(
                            "[%s] Groq returned malformed tool call arguments for '%s' "
                            "(len=%d): %s",
                            self.name, tc.function.name,
                            len(tc.function.arguments),
                            tc.function.arguments[:200],
                        )
                        args = {}
                    call = ToolCall(tool_name=tc.function.name, arguments=args, call_id=tc.id)
                    result = await self.registry.execute(call)
                    self._metrics.total_tool_calls += 1
                    if not result.success:
                        self._metrics.tool_failures += 1
                    logger.info("[%s] Tool %s → %s (%dms)", self.name, tc.function.name, "OK" if result.success else "FAIL", result.duration_ms)
                    content = _truncate_tool_output(result.data) if result.success else json.dumps({"error": result.error})
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})
                continue

            final = choice.message.content or ""
            if output_validator and validation_retries < MAX_VALIDATION_RETRIES:
                is_valid, err = output_validator.validate(final)
                if not is_valid:
                    validation_retries += 1
                    self._metrics.validation_retries += 1
                    logger.warning("[%s] Validation failed (retry %d): %s", self.name, validation_retries, err)
                    messages.append({"role": "assistant", "content": final})
                    if not final.strip():
                        retry_msg = "Your response was empty. You MUST output a complete JSON object with all required keys. Return ONLY the JSON object now."
                    else:
                        retry_msg = f"INVALID output: {err}\nFix it. Return ONLY corrected JSON."
                    messages.append({"role": "user", "content": retry_msg})
                    continue
            logger.info("[%s] Groq ReAct done: %d iters", self.name, iteration)
            return final

        logger.warning("[%s] Hit max iterations (%d)", self.name, max_iterations)
        return '{"error": "Agent reached maximum reasoning iterations"}'

    async def _call_groq(self, system_prompt, user_prompt, *, model, temperature, max_tokens) -> str:
        t0 = time.monotonic()
        response = await self.groq_client.chat.completions.create(
            model=model, max_tokens=max_tokens,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            temperature=temperature,
        )
        elapsed = int((time.monotonic() - t0) * 1000)
        content = response.choices[0].message.content or ""
        usage = response.usage
        self._metrics.total_llm_calls += 1
        self._metrics.total_prompt_tokens += usage.prompt_tokens if usage else 0
        self._metrics.total_completion_tokens += usage.completion_tokens if usage else 0
        self._record_reasoning(thinking=content[:500], model=model, provider="groq",
                               prompt_tokens=usage.prompt_tokens if usage else 0,
                               completion_tokens=usage.completion_tokens if usage else 0, duration_ms=elapsed)
        return content

    # ─── Tool Schema Conversion ────────────────────────────────────────

    def _registry_to_openai_tools(self) -> list[dict]:
        tools = []
        for tool in self.registry.tools:
            properties = {}
            required = []
            for param in tool.parameters:
                prop: dict[str, Any] = {"type": param.param_type, "description": param.description}
                if param.enum:
                    prop["enum"] = param.enum
                properties[param.name] = prop
                if param.required:
                    required.append(param.name)
            schema: dict[str, Any] = {
                "type": "function",
                "function": {
                    "name": tool.name, "description": tool.description,
                    "parameters": {"type": "object", "properties": properties},
                },
            }
            if required:
                schema["function"]["parameters"]["required"] = required
            tools.append(schema)
        return tools

    # ─── Observability ─────────────────────────────────────────────────

    def _record_reasoning(
        self, thinking: str, model: str, provider: str,
        prompt_tokens: int, completion_tokens: int,
        duration_ms: int, iteration: int | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "agent": self.name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "thinking": thinking,
            "token_usage": {
                "model": model, "provider": provider,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "duration_ms": duration_ms,
            },
        }
        if iteration is not None:
            entry["iteration"] = iteration
        self._reasoning_entries.append(entry)

    def get_metrics_summary(self) -> dict:
        """Return a summary of this agent run's metrics."""
        m = self._metrics
        return {
            "agent": self.name,
            "llm_calls": m.total_llm_calls,
            "tool_calls": m.total_tool_calls,
            "tool_failures": m.tool_failures,
            "prompt_tokens": m.total_prompt_tokens,
            "completion_tokens": m.total_completion_tokens,
            "total_tokens": m.total_prompt_tokens + m.total_completion_tokens,
            "duration_ms": m.total_duration_ms,
            "validation_retries": m.validation_retries,
            "provider_fallbacks": m.provider_fallbacks,
            "compression_count": m.compression_count,
            "providers_used": m.providers_used,
        }


# ─── Helpers ───────────────────────────────────────────────────────────

def _first_text_anthropic(content_blocks) -> str:
    for block in content_blocks:
        if hasattr(block, "text"):
            return block.text
    return ""


def _extract_json(raw: str) -> str:
    stripped = _strip_fences(raw.strip())
    if not stripped:
        return stripped
    if stripped[0] in "{[":
        return stripped
    span = _find_balanced_json(stripped)
    return span if span is not None else stripped


def _find_balanced_json(text: str) -> str | None:
    """Return the first balanced JSON object or array in `text`.

    Bracket-counting scan that ignores braces/brackets inside string
    literals and escape sequences. Safer than a greedy regex when the
    LLM emits prose with multiple JSON fragments.
    """
    start_pairs = {"{": "}", "[": "]"}
    for i, ch in enumerate(text):
        if ch in start_pairs:
            close = start_pairs[ch]
            depth = 0
            in_string = False
            escape = False
            for j in range(i, len(text)):
                c = text[j]
                if in_string:
                    if escape:
                        escape = False
                    elif c == "\\":
                        escape = True
                    elif c == '"':
                        in_string = False
                    continue
                if c == '"':
                    in_string = True
                    continue
                if c == ch:
                    depth += 1
                elif c == close:
                    depth -= 1
                    if depth == 0:
                        return text[i : j + 1]
            # Unbalanced — keep scanning for a later candidate.
    return None


def repair_truncated_json(raw: str) -> str | None:
    """Repair JSON truncated mid-generation by closing strings and brackets."""
    stripped = raw.strip()
    if not stripped:
        return None

    if stripped[-1] == '"':
        pass
    elif stripped[-1] not in ("}", "]", '"'):
        stripped += '"'

    brackets = {"{": "}", "[": "]"}
    stack: list[str] = []
    in_string = False
    escape = False
    for ch in stripped:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in brackets:
            stack.append(brackets[ch])
        elif ch == "}":
            if stack and stack[-1] == "}":
                stack.pop()
        elif ch == "]":
            if stack and stack[-1] == "]":
                stack.pop()

    repaired = stripped + "".join(reversed(stack))
    if len(repaired) < len(raw.strip()):
        return None
    return repaired
