from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from qdrant_client import AsyncQdrantClient, models
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    entity_id: str
    score: float
    payload: dict[str, Any]


_POINT_ID_NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # DNS namespace
_UPSERT_BATCH_SIZE = 64

# Qdrant client raises generic Exception for transient errors; we retry on
# everything except validation-style errors (ValueError, TypeError).
_RETRYABLE = (ConnectionError, TimeoutError, OSError)


def _to_point_id(entity_id: str) -> str:
    return str(uuid.uuid5(_POINT_ID_NS, entity_id))


# Suffixes appended to entity_id to generate per-chunk-type point IDs.
# "" = summary (backward-compatible), ":bs" = behavioral_signals, ":an" = anomalies.
_CHUNK_SUFFIXES = ("", ":bs", ":an")


def _all_chunk_point_ids(entity_id: str) -> list[str]:
    """Return point IDs for all hierarchical chunk variants of an entity."""
    return [_to_point_id(entity_id + s) for s in _CHUNK_SUFFIXES]


def memory_point_id(user_id: str, content: str) -> str:
    """Stable UUID per (user, content) — same fact gets the same point id (natural dedup)."""
    return str(uuid.uuid5(_POINT_ID_NS, f"mem:{user_id}:{content[:200]}"))


def _retry() -> AsyncRetrying:
    return AsyncRetrying(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=4.0),
        retry=retry_if_exception_type(_RETRYABLE),
        reraise=True,
    )


class QdrantService:
    """Async Qdrant wrapper with batched upserts and retries on transients."""

    def __init__(self) -> None:
        self.client: AsyncQdrantClient | None = None

    async def _get_client(self) -> AsyncQdrantClient:
        if self.client is None:
            self.client = AsyncQdrantClient(
                url=settings.QDRANT_URL,
                api_key=settings.QDRANT_API_KEY,
            )
        return self.client

    async def ensure_collection(self, org_id: str) -> None:
        client = await self._get_client()
        collection_name = settings.get_org_collection_name(org_id)
        if await client.collection_exists(collection_name):
            return
        await client.create_collection(
            collection_name=collection_name,
            vectors_config=models.VectorParams(
                size=settings.QDRANT_VECTOR_SIZE,
                distance=models.Distance.COSINE,
            ),
            quantization_config=models.ScalarQuantization(
                scalar=models.ScalarQuantizationConfig(type=models.ScalarType.INT8),
            ),
        )
        # Indexes enable metadata filtering and hybrid keyword search; safe to
        # create alongside the collection. Each call is idempotent server-side.
        try:
            await client.create_payload_index(
                collection_name=collection_name,
                field_name="profile_summary",
                field_schema=models.TextIndexParams(
                    type=models.TextIndexType.TEXT,
                    tokenizer=models.TokenizerType.WORD,
                    lowercase=True,
                ),
            )
            for keyword_field in ("risk_tier", "status", "model_version", "chunk_type", "entity_id"):
                await client.create_payload_index(
                    collection_name=collection_name,
                    field_name=keyword_field,
                    field_schema=models.PayloadSchemaType.KEYWORD,
                )
            for float_field in ("last_scored_at", "embedded_at"):
                await client.create_payload_index(
                    collection_name=collection_name,
                    field_name=float_field,
                    field_schema=models.PayloadSchemaType.FLOAT,
                )
        except Exception as exc:
            logger.warning("Payload index creation skipped: %s", exc)
        logger.info("Created Qdrant collection %s for org %s", collection_name, org_id)

    async def upsert_entity(
        self,
        org_id: str,
        entity_id: str,
        vector: list[float],
        payload: dict[str, Any],
    ) -> None:
        await self.upsert_batch(org_id, [(entity_id, vector, payload)])

    async def upsert_batch(
        self,
        org_id: str,
        items: Sequence[tuple[str, list[float], dict[str, Any]]],
    ) -> None:
        """Batch upsert with retry on transients. Each item is (id, vector, payload)."""
        if not items:
            return
        client = await self._get_client()
        collection_name = settings.get_org_collection_name(org_id)

        points = [
            models.PointStruct(
                id=_to_point_id(entity_id),
                vector=vector,
                payload={**payload, "_entity_id": entity_id},
            )
            for entity_id, vector, payload in items
        ]

        async for attempt in _retry():
            with attempt:
                t0 = time.perf_counter()
                for start in range(0, len(points), _UPSERT_BATCH_SIZE):
                    chunk = points[start : start + _UPSERT_BATCH_SIZE]
                    await client.upsert(collection_name=collection_name, points=chunk)
                logger.info(
                    "[Qdrant] upserted %d points to %s in %.0fms",
                    len(points),
                    collection_name,
                    (time.perf_counter() - t0) * 1000,
                )

    async def set_entity_payload(
        self,
        org_id: str,
        entity_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Partial payload update for an entity. Applies to all chunk variants."""
        client = await self._get_client()
        collection_name = settings.get_org_collection_name(org_id)
        async for attempt in _retry():
            with attempt:
                await client.set_payload(
                    collection_name=collection_name,
                    payload=payload,
                    points=_all_chunk_point_ids(entity_id),
                )

    async def set_payload_batch(
        self,
        org_id: str,
        updates: Iterable[tuple[str, dict[str, Any]]],
    ) -> None:
        """Apply per-entity payload patches sequentially with retry."""
        for entity_id, payload in updates:
            try:
                await self.set_entity_payload(org_id, entity_id, payload)
            except Exception as exc:
                logger.warning(
                    "[Qdrant] set_payload failed for %s: %s", entity_id, exc
                )

    async def search_similar(
        self,
        org_id: str,
        vector: list[float],
        *,
        limit: int = 10,
        score_threshold: float | None = None,
        filter_condition: models.Filter | None = None,
    ) -> list[SearchResult]:
        client = await self._get_client()
        collection_name = settings.get_org_collection_name(org_id)
        t0 = time.perf_counter()
        async for attempt in _retry():
            with attempt:
                results = await client.query_points(
                    collection_name=collection_name,
                    query=vector,
                    query_filter=filter_condition,
                    limit=limit,
                    score_threshold=score_threshold,
                    with_payload=True,
                )
        logger.debug(
            "[Qdrant] search_similar %s limit=%d took %.0fms returned=%d",
            collection_name,
            limit,
            (time.perf_counter() - t0) * 1000,
            len(results.points),
        )
        return [
            SearchResult(
                entity_id=(p.payload or {}).get("entity_id") or (p.payload or {}).get("_entity_id", str(p.id)),
                score=p.score,
                payload=p.payload or {},
            )
            for p in results.points
        ]

    async def hybrid_search(
        self,
        org_id: str,
        vector: list[float],
        *,
        text_query: str | None = None,
        limit: int = 10,
        prefetch_limit: int = 40,
        filter_condition: models.Filter | None = None,
    ) -> list[SearchResult]:
        """Dense + keyword fused via Reciprocal Rank Fusion.

        Keyword stage uses Qdrant `MatchText` on the indexed `profile_summary`
        payload field. Falls back to dense-only if no text query supplied.
        """
        if not text_query:
            return await self.search_similar(
                org_id,
                vector,
                limit=limit,
                filter_condition=filter_condition,
            )

        client = await self._get_client()
        collection_name = settings.get_org_collection_name(org_id)

        text_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="profile_summary",
                    match=models.MatchText(text=text_query),
                )
            ]
        )

        prefetch = [
            models.Prefetch(query=vector, limit=prefetch_limit),
            models.Prefetch(filter=text_filter, limit=prefetch_limit),
        ]

        t0 = time.perf_counter()
        try:
            results = await client.query_points(
                collection_name=collection_name,
                prefetch=prefetch,
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                query_filter=filter_condition,
                limit=limit,
                with_payload=True,
            )
        except Exception as exc:
            logger.warning(
                "[Qdrant] hybrid_search failed, falling back to dense: %s", exc
            )
            return await self.search_similar(
                org_id, vector, limit=limit, filter_condition=filter_condition
            )

        logger.debug(
            "[Qdrant] hybrid_search %s limit=%d took %.0fms returned=%d",
            collection_name,
            limit,
            (time.perf_counter() - t0) * 1000,
            len(results.points),
        )
        return [
            SearchResult(
                entity_id=(p.payload or {}).get("entity_id") or (p.payload or {}).get("_entity_id", str(p.id)),
                score=p.score,
                payload=p.payload or {},
            )
            for p in results.points
        ]

    async def archive_stale_points(self, org_id: str, ttl_days: int) -> int:
        """Delete points whose embedded_at is older than ttl_days. Returns removed count."""
        client = await self._get_client()
        collection_name = settings.get_org_collection_name(org_id)
        if not await client.collection_exists(collection_name):
            return 0

        cutoff = time.time() - ttl_days * 86400
        stale_ids: list = []
        offset = None

        while True:
            async for attempt in _retry():
                with attempt:
                    records, next_offset = await client.scroll(
                        collection_name=collection_name,
                        scroll_filter=None,
                        limit=256,
                        offset=offset,
                        with_payload=True,
                        with_vectors=False,
                    )
            for record in records:
                embedded_at = (record.payload or {}).get("embedded_at")
                if embedded_at is not None and float(embedded_at) < cutoff:
                    stale_ids.append(record.id)
            if next_offset is None:
                break
            offset = next_offset

        if not stale_ids:
            return 0

        async for attempt in _retry():
            with attempt:
                await client.delete(
                    collection_name=collection_name,
                    points_selector=models.PointIdsList(points=stale_ids),
                )
        logger.info(
            "[Qdrant] TTL cleanup: removed %d stale points from %s",
            len(stale_ids),
            collection_name,
        )
        return len(stale_ids)

    async def get_collection_stats(self, org_id: str) -> dict:
        """Return point count and vector stats for monitoring."""
        client = await self._get_client()
        collection_name = settings.get_org_collection_name(org_id)
        try:
            if not await client.collection_exists(collection_name):
                return {}
            info = await client.get_collection(collection_name)
            return {
                "collection": collection_name,
                "points_count": info.points_count or 0,
                "indexed_vectors_count": info.indexed_vectors_count or 0,
            }
        except Exception as exc:
            logger.debug("[Qdrant] get_collection_stats failed: %s", exc)
            return {}

    async def remove_entity(self, org_id: str, entity_id: str) -> None:
        client = await self._get_client()
        collection_name = settings.get_org_collection_name(org_id)
        await client.delete(
            collection_name=collection_name,
            points_selector=models.PointIdsList(points=_all_chunk_point_ids(entity_id)),
        )

    async def delete_org_collection(self, org_id: str) -> None:
        client = await self._get_client()
        collection_name = settings.get_org_collection_name(org_id)
        if await client.collection_exists(collection_name):
            await client.delete_collection(collection_name)
            logger.info("Deleted Qdrant collection %s", collection_name)

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = None

    # ─── Conversational memory collection ──────────────────────────────
    # Separate collection per org for episodic / semantic / procedural memory entries.
    # Distinct from the entity-profile collection so the two have independent
    # lifecycles (retention, prune, schema).

    async def ensure_memory_collection(self, org_id: str) -> None:
        client = await self._get_client()
        collection_name = settings.get_org_memory_collection_name(org_id)
        if await client.collection_exists(collection_name):
            return
        await client.create_collection(
            collection_name=collection_name,
            vectors_config=models.VectorParams(
                size=settings.QDRANT_VECTOR_SIZE,
                distance=models.Distance.COSINE,
            ),
            quantization_config=models.ScalarQuantization(
                scalar=models.ScalarQuantizationConfig(type=models.ScalarType.INT8),
            ),
        )
        try:
            for keyword_field in ("user_id", "kind", "source"):
                await client.create_payload_index(
                    collection_name=collection_name,
                    field_name=keyword_field,
                    field_schema=models.PayloadSchemaType.KEYWORD,
                )
            for float_field in ("importance", "embedded_at"):
                await client.create_payload_index(
                    collection_name=collection_name,
                    field_name=float_field,
                    field_schema=models.PayloadSchemaType.FLOAT,
                )
        except Exception as exc:
            logger.warning("Memory payload index creation skipped: %s", exc)
        logger.info("Created Qdrant memory collection %s for org %s", collection_name, org_id)

    async def upsert_memory(
        self,
        org_id: str,
        items: Sequence[tuple[str, list[float], dict[str, Any]]],
    ) -> None:
        """Upsert memory points. Each item is (point_id, vector, payload)."""
        if not items:
            return
        client = await self._get_client()
        collection_name = settings.get_org_memory_collection_name(org_id)
        points = [
            models.PointStruct(id=pid, vector=vec, payload=payload)
            for pid, vec, payload in items
        ]
        async for attempt in _retry():
            with attempt:
                for start in range(0, len(points), _UPSERT_BATCH_SIZE):
                    chunk = points[start : start + _UPSERT_BATCH_SIZE]
                    await client.upsert(collection_name=collection_name, points=chunk)
        logger.debug("[Qdrant] memory upsert %d points to %s", len(points), collection_name)

    async def search_memory(
        self,
        org_id: str,
        vector: list[float],
        *,
        user_id: str,
        limit: int = 3,
        score_threshold: float | None = None,
    ) -> list[SearchResult]:
        """Per-user semantic search over the memory collection."""
        client = await self._get_client()
        collection_name = settings.get_org_memory_collection_name(org_id)
        if not await client.collection_exists(collection_name):
            return []
        user_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="user_id", match=models.MatchValue(value=user_id)
                )
            ]
        )
        async for attempt in _retry():
            with attempt:
                results = await client.query_points(
                    collection_name=collection_name,
                    query=vector,
                    query_filter=user_filter,
                    limit=limit,
                    score_threshold=score_threshold,
                    with_payload=True,
                )
        return [
            SearchResult(
                entity_id=str(p.id),
                score=p.score,
                payload=p.payload or {},
            )
            for p in results.points
        ]

    async def prune_memory(
        self,
        org_id: str,
        *,
        importance_below: float | None = None,
        older_than_days: int | None = None,
    ) -> int:
        """Delete memory points matching importance/age filters. Returns removed count."""
        client = await self._get_client()
        collection_name = settings.get_org_memory_collection_name(org_id)
        if not await client.collection_exists(collection_name):
            return 0

        must: list = []
        if importance_below is not None:
            must.append(
                models.FieldCondition(
                    key="importance",
                    range=models.Range(lt=float(importance_below)),
                )
            )
        if older_than_days is not None and older_than_days > 0:
            cutoff = time.time() - older_than_days * 86400
            must.append(
                models.FieldCondition(
                    key="embedded_at",
                    range=models.Range(lt=cutoff),
                )
            )
        if not must:
            return 0

        # Scroll once to count what will be deleted, then delete by filter.
        # Counting via scroll is bounded by limit; for true count we'd need a separate count call.
        # We opt for a delete-by-filter and report via a follow-up count.
        before = (await self._memory_point_count(org_id)) or 0
        prune_filter = models.Filter(must=must)
        async for attempt in _retry():
            with attempt:
                await client.delete(
                    collection_name=collection_name,
                    points_selector=models.FilterSelector(filter=prune_filter),
                )
        after = (await self._memory_point_count(org_id)) or 0
        removed = max(0, before - after)
        if removed:
            logger.info("[Qdrant] memory prune removed %d points from %s", removed, collection_name)
        return removed

    async def _memory_point_count(self, org_id: str) -> int | None:
        client = await self._get_client()
        collection_name = settings.get_org_memory_collection_name(org_id)
        try:
            info = await client.get_collection(collection_name)
            return int(info.points_count or 0)
        except Exception:
            return None
