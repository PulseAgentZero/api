"""LLM client factory. Returns the configured provider based on AI_PROVIDER env var.

Supported providers:
  anthropic    — Anthropic Claude (default)
  openai       — OpenAI GPT-4o / GPT-4o-mini
  groq         — Groq (llama-3.3-70b / llama-3.1-8b)
  ollama       — Local Ollama server (llama3, mistral, etc.)
  azure_openai — Azure OpenAI Service
  mistral      — Mistral AI (mistral-large / mistral-small)
  google       — Google Gemini (gemini-1.5-pro / gemini-1.5-flash)

Auto-detection: uses the first provider that has a configured API key.
"""

from __future__ import annotations

import logging
import os

from app.infrastructure.llm.base import LLMClient

logger = logging.getLogger(__name__)

_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """Return the singleton LLM client. Cached after first call."""
    global _client
    if _client is None:
        _client = _create_client()
    return _client


def _create_client() -> LLMClient:  # noqa: C901
    from app.infrastructure.llm.providers import (
        AnthropicClient,
        AzureOpenAIClient,
        OpenAICompatibleClient,
    )

    provider = os.getenv("AI_PROVIDER", "").strip().lower()

    # ── Auto-detect from available API keys ───────────────────────────────────
    if not provider:
        if os.getenv("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        elif os.getenv("OPENAI_API_KEY"):
            provider = "openai"
        elif os.getenv("GROQ_API_KEY"):
            provider = "groq"
        elif os.getenv("MISTRAL_API_KEY"):
            provider = "mistral"
        elif os.getenv("GOOGLE_AI_API_KEY"):
            provider = "google"
        elif os.getenv("AZURE_OPENAI_API_KEY"):
            provider = "azure_openai"
        elif os.getenv("OLLAMA_BASE_URL"):
            provider = "ollama"
        else:
            provider = "anthropic"  # will fail gracefully when called

    logger.info("LLM provider: %s", provider)

    if provider == "anthropic":
        return AnthropicClient(
            api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            model=os.getenv("AI_MODEL", "claude-sonnet-4-6"),
            fast_model=os.getenv("AI_FAST_MODEL", "claude-haiku-4-5-20251001"),
        )

    if provider == "openai":
        return OpenAICompatibleClient(
            api_key=os.getenv("OPENAI_API_KEY", ""),
            model=os.getenv("AI_MODEL", os.getenv("OPENAI_LLM_MODEL", "gpt-4o")),
            fast_model=os.getenv("AI_FAST_MODEL", os.getenv("OPENAI_LLM_MODEL_FAST", "gpt-4o-mini")),
            provider="openai",
        )

    if provider == "groq":
        return OpenAICompatibleClient(
            api_key=os.getenv("GROQ_API_KEY", ""),
            model=os.getenv("AI_MODEL", "llama-3.3-70b-versatile"),
            fast_model=os.getenv("AI_FAST_MODEL", "llama-3.1-8b-instant"),
            base_url="https://api.groq.com/openai/v1",
            provider="groq",
        )

    if provider == "ollama":
        return OpenAICompatibleClient(
            api_key="ollama",  # Ollama does not require a real key
            model=os.getenv("AI_MODEL", os.getenv("OLLAMA_MODEL", "llama3.2")),
            fast_model=os.getenv("AI_FAST_MODEL", os.getenv("OLLAMA_MODEL_FAST", os.getenv("OLLAMA_MODEL", "llama3.2"))),
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434") + "/v1",
            provider="ollama",
        )

    if provider == "azure_openai":
        return AzureOpenAIClient(
            api_key=os.getenv("AZURE_OPENAI_API_KEY", ""),
            endpoint=os.getenv("AZURE_OPENAI_ENDPOINT", ""),
            deployment=os.getenv("AI_MODEL", os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01"),
            fast_deployment=os.getenv("AI_FAST_MODEL", os.getenv("AZURE_OPENAI_FAST_DEPLOYMENT", "")),
        )

    if provider == "mistral":
        return OpenAICompatibleClient(
            api_key=os.getenv("MISTRAL_API_KEY", ""),
            model=os.getenv("AI_MODEL", "mistral-large-latest"),
            fast_model=os.getenv("AI_FAST_MODEL", "mistral-small-latest"),
            base_url="https://api.mistral.ai/v1",
            provider="mistral",
        )

    if provider == "google":
        # Google Gemini has an OpenAI-compatible endpoint
        return OpenAICompatibleClient(
            api_key=os.getenv("GOOGLE_AI_API_KEY", ""),
            model=os.getenv("AI_MODEL", "gemini-1.5-pro"),
            fast_model=os.getenv("AI_FAST_MODEL", "gemini-1.5-flash"),
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            provider="google",
        )

    # Unknown provider — default to Anthropic with a warning
    logger.warning("Unknown AI_PROVIDER=%r — falling back to anthropic", provider)
    return AnthropicClient(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
