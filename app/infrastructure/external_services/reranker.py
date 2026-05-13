"""Voyage rerank-2-lite client: reorder retrieved candidates by query relevance."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from app.config.settings import settings

VOYAGE_RERANK_URL = "https://api.voyageai.com/v1/rerank"
MAX_RETRIES = 3

logger = logging.getLogger(__name__)


class RerankError(Exception):
    pass


class VoyageReranker:
    """Lightweight async wrapper around Voyage's rerank endpoint."""

    def __init__(self, model: str | None = None) -> None:
        self.model = model or settings.VOYAGE_RERANK_MODEL
        self._api_key: str | None = None
        self._initialized = False

    def _ensure_initialized(self) -> bool:
        if self._initialized:
            return True
        self._api_key = settings.get_voyageai_api_key()
        if self._api_key:
            self._initialized = True
            return True
        logger.debug("VOYAGEAI_API_KEY not set — reranker disabled")
        return False

    async def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int | None = None,
    ) -> list[tuple[int, float]]:
        """Return (original_index, relevance_score) tuples in ranked order."""
        if not documents:
            return []
        if not self._ensure_initialized():
            raise RerankError("VOYAGEAI_API_KEY not configured")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "query": query,
            "documents": documents,
        }
        if top_n is not None:
            payload["top_k"] = top_n

        for attempt in range(MAX_RETRIES):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        VOYAGE_RERANK_URL,
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        if resp.status == 429:
                            wait = float(resp.headers.get("Retry-After", "2"))
                            await asyncio.sleep(wait)
                            continue
                        body = await resp.json()
                        if resp.status != 200:
                            if attempt < MAX_RETRIES - 1:
                                await asyncio.sleep(2**attempt)
                                continue
                            raise RerankError(
                                f"Voyage rerank returned {resp.status}: {body}"
                            )
                        data = body.get("data", [])
                        results = [
                            (int(item["index"]), float(item["relevance_score"]))
                            for item in data
                        ]
                        # Voyage already returns results in ranked order; sort
                        # defensively in case the API changes.
                        results.sort(key=lambda x: x[1], reverse=True)
                        return results
            except asyncio.TimeoutError:
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2**attempt)
                continue
            except aiohttp.ClientError as exc:
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2**attempt)
                    continue
                raise RerankError(f"Rerank connection error: {exc}") from exc

        raise RerankError(f"Rerank failed after {MAX_RETRIES} retries")


voyage_reranker = VoyageReranker()
