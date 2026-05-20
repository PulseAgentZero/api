"""All LLM provider implementations.

Providers using the OpenAI-compatible API (OpenAI, Groq, Ollama, Mistral, Azure OpenAI,
Google Gemini) share one implementation — they differ only in base_url, api_key, and
model names.

Anthropic uses its own SDK because its message format differs slightly.
"""

from __future__ import annotations

import logging
import os

from app.infrastructure.llm.base import LLMClient

logger = logging.getLogger(__name__)


# ── Anthropic ─────────────────────────────────────────────────────────────────

class AnthropicClient(LLMClient):
    """Anthropic Claude (claude-sonnet-4-6, claude-haiku-4-5, claude-opus-4-6)."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6", fast_model: str = "claude-haiku-4-5-20251001") -> None:
        self._api_key = api_key
        self._model = model
        self._fast = fast_model

    def is_configured(self) -> bool:
        return bool(self._api_key)

    @property
    def default_model(self) -> str:
        return self._model

    @property
    def fast_model(self) -> str:
        return self._fast

    async def complete(self, system: str, user: str, *, max_tokens: int = 1000, temperature: float = 0.1, model: str | None = None) -> str:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=self._api_key)
        response = await client.messages.create(
            model=model or self._model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in response.content if hasattr(b, "text")).strip()


# ── OpenAI-compatible base (reused by OpenAI, Groq, Ollama, Mistral, etc.) ───

class OpenAICompatibleClient(LLMClient):
    """OpenAI-compatible API client. Works with OpenAI, Groq, Ollama, Mistral, Azure, Gemini."""

    def __init__(
        self,
        api_key: str,
        model: str,
        fast_model: str,
        base_url: str | None = None,
        provider: str = "openai",
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._fast = fast_model
        self._base_url = base_url
        self._provider = provider

    def is_configured(self) -> bool:
        return bool(self._api_key or self._provider == "ollama")

    @property
    def default_model(self) -> str:
        return self._model

    @property
    def fast_model(self) -> str:
        return self._fast

    @property
    def provider_name(self) -> str:
        return self._provider

    async def complete(self, system: str, user: str, *, max_tokens: int = 1000, temperature: float = 0.1, model: str | None = None) -> str:
        from openai import AsyncOpenAI
        kwargs: dict = {"api_key": self._api_key or "ollama"}
        if self._base_url:
            kwargs["base_url"] = self._base_url
        client = AsyncOpenAI(**kwargs)
        response = await client.chat.completions.create(
            model=model or self._model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return (response.choices[0].message.content or "").strip()


# ── Azure OpenAI ──────────────────────────────────────────────────────────────

class AzureOpenAIClient(LLMClient):
    """Azure OpenAI Service."""

    def __init__(self, api_key: str, endpoint: str, deployment: str, api_version: str = "2024-02-01", fast_deployment: str | None = None) -> None:
        self._api_key = api_key
        self._endpoint = endpoint.rstrip("/")
        self._deployment = deployment
        self._fast_deployment = fast_deployment or deployment
        self._api_version = api_version

    def is_configured(self) -> bool:
        return bool(self._api_key and self._endpoint and self._deployment)

    @property
    def default_model(self) -> str:
        return self._deployment

    @property
    def fast_model(self) -> str:
        return self._fast_deployment

    @property
    def provider_name(self) -> str:
        return "azure_openai"

    async def complete(self, system: str, user: str, *, max_tokens: int = 1000, temperature: float = 0.1, model: str | None = None) -> str:
        from openai import AsyncAzureOpenAI
        client = AsyncAzureOpenAI(
            api_key=self._api_key,
            azure_endpoint=self._endpoint,
            api_version=self._api_version,
        )
        response = await client.chat.completions.create(
            model=model or self._deployment,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return (response.choices[0].message.content or "").strip()
