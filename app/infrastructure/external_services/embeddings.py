from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from app.config.settings import settings

VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"
MAX_RETRIES = 3
BATCH_SIZE = 96

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    pass


class EmbeddingService:
    def __init__(self) -> None:
        self.model = settings.EMBEDDING_MODEL
        self.dimension = settings.EMBEDDING_DIMENSION
        self._api_key: str | None = None
        self._initialized = False

    def _ensure_initialized(self) -> bool:
        if self._initialized:
            return True
        self._api_key = settings.get_voyageai_api_key()
        if self._api_key:
            self._initialized = True
            logger.info(
                "Embedding service initialized with %s (%d dims)",
                self.model,
                self.dimension,
            )
            return True
        logger.warning("VOYAGEAI_API_KEY not set — embedding service unavailable")
        return False

    async def _call_voyage(
        self,
        texts: list[str],
        input_type: str,
    ) -> list[list[float] | None]:
        if not self._ensure_initialized():
            raise EmbeddingError("VOYAGEAI_API_KEY not configured")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "input": texts,
            "input_type": input_type,
        }

        for attempt in range(MAX_RETRIES):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        VOYAGE_API_URL,
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=120),
                    ) as resp:
                        if resp.status == 429:
                            retry_after = resp.headers.get("Retry-After", "2")
                            wait = float(retry_after)
                            logger.warning(
                                "Rate limited (429), waiting %.1fs (attempt %d/%d)",
                                wait, attempt + 1, MAX_RETRIES,
                            )
                            await asyncio.sleep(wait)
                            continue

                        body = await resp.json()
                        if resp.status != 200:
                            logger.error(
                                "Voyage API error %d: %s", resp.status, body
                            )
                            if attempt < MAX_RETRIES - 1:
                                await asyncio.sleep(2**attempt)
                                continue
                            raise EmbeddingError(
                                f"Voyage API returned {resp.status}: {body}"
                            )

                        data = body.get("data", [])
                        results: list[list[float] | None] = [None] * len(texts)
                        for item in data:
                            idx = item["index"]
                            results[idx] = item["embedding"]

                        ok = sum(1 for r in results if r is not None)
                        logger.debug(
                            "Embedded %d/%d texts (%d dims)",
                            ok, len(texts), self.dimension,
                        )
                        return results

            except asyncio.TimeoutError:
                logger.warning(
                    "Voyage API timeout (attempt %d/%d)",
                    attempt + 1, MAX_RETRIES,
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2**attempt)
                continue
            except aiohttp.ClientError as e:
                logger.error("Voyage API connection error: %s", e)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2**attempt)
                continue

        raise EmbeddingError(f"Embedding failed after {MAX_RETRIES} retries")

    async def embed_document(self, text: str) -> list[float]:
        results = await self._call_voyage([text], input_type="document")
        vector = results[0]
        if vector is None:
            raise EmbeddingError("Failed to embed document")
        return vector

    async def embed_query(self, text: str) -> list[float]:
        results = await self._call_voyage([text], input_type="query")
        vector = results[0]
        if vector is None:
            raise EmbeddingError("Failed to embed query")
        return vector

    async def embed_batch(
        self, texts: list[str], input_type: str = "document"
    ) -> list[list[float]]:
        all_results: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            batch = texts[start : start + BATCH_SIZE]
            batch_results = await self._call_voyage(batch, input_type=input_type)
            for r in batch_results:
                if r is None:
                    raise EmbeddingError(
                        f"Failed to embed text at index {start + len(all_results)}"
                    )
                all_results.append(r)
            if start + BATCH_SIZE < len(texts):
                await asyncio.sleep(0.1)
        return all_results


embedding_service = EmbeddingService()
