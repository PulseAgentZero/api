"""Abstract base for all LLM clients used by Studio AI and agent features."""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMClient(ABC):
    """Unified interface for text completion across all supported LLM providers."""

    @abstractmethod
    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 1000,
        temperature: float = 0.1,
        model: str | None = None,
    ) -> str:
        """Return the assistant's text response."""

    @abstractmethod
    def is_configured(self) -> bool:
        """Return True if the required API key / endpoint is available."""

    @property
    @abstractmethod
    def default_model(self) -> str:
        """The default model name for this provider."""

    @property
    @abstractmethod
    def fast_model(self) -> str:
        """A faster/cheaper model for low-stakes calls (explanation, quick classify)."""

    @property
    def provider_name(self) -> str:
        return self.__class__.__name__.replace("Client", "").lower()
