"""Async Qdrant wrapper for the hackathon item collection."""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Sequence

from qdrant_client import AsyncQdrantClient, models

from app.config.settings import settings
from hackathon.config import HACKATHON_QDRANT_COLLECTION, VECTOR_SIZE

logger = logging.getLogger(__name__)

ItemPayload = dict[str, Any]
ItemRow = tuple[str, list[float], ItemPayload]

_UPSERT_BATCH_SIZE = 64


def _point_id(dataset: str, item_id: str) -> str:
    return hashlib.md5(f"{dataset}:{item_id}".encode("utf-8")).hexdigest()


class HackathonVectorStore:
    """Thin facade over the Qdrant client scoped to one collection."""

    def __init__(self, collection: str = HACKATHON_QDRANT_COLLECTION) -> None:
        self.collection = collection
        self._client: AsyncQdrantClient | None = None

    async def client(self) -> AsyncQdrantClient:
        if self._client is None:
            self._client = AsyncQdrantClient(
                url=settings.QDRANT_URL,
                api_key=settings.QDRANT_API_KEY,
                check_compatibility=False,
            )
        return self._client

    async def ensure_collection(self) -> None:
        client = await self.client()
        if await client.collection_exists(self.collection):
            return
        await client.create_collection(
            collection_name=self.collection,
            vectors_config=models.VectorParams(
                size=VECTOR_SIZE,
                distance=models.Distance.COSINE,
            ),
        )
        for field in ("dataset", "item_id"):
            await client.create_payload_index(
                collection_name=self.collection,
                field_name=field,
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
        logger.info("Created Qdrant collection %s", self.collection)

    async def reset_collection(self) -> None:
        client = await self.client()
        if await client.collection_exists(self.collection):
            await client.delete_collection(collection_name=self.collection)
        await self.ensure_collection()

    async def upsert_items(self, rows: Sequence[ItemRow]) -> None:
        if not rows:
            return
        client = await self.client()
        points = [
            models.PointStruct(
                id=_point_id(payload["dataset"], payload["item_id"]),
                vector=vector,
                payload=payload,
            )
            for _, vector, payload in rows
        ]
        for start in range(0, len(points), _UPSERT_BATCH_SIZE):
            await client.upsert(
                collection_name=self.collection,
                points=points[start : start + _UPSERT_BATCH_SIZE],
            )

    async def search(
        self,
        vector: list[float],
        *,
        k: int = 50,
        dataset: str | None = None,
        exclude_item_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        client = await self.client()
        query_filter = self._build_filter(dataset, exclude_item_ids)
        result = await client.query_points(
            collection_name=self.collection,
            query=vector,
            query_filter=query_filter,
            limit=k,
            with_payload=True,
        )
        return [self._payload_to_dict(p) for p in result.points]

    @staticmethod
    def _build_filter(
        dataset: str | None,
        exclude_item_ids: Sequence[str] | None,
    ) -> models.Filter | None:
        must: list[models.FieldCondition] = []
        must_not: list[models.FieldCondition] = []
        if dataset:
            must.append(
                models.FieldCondition(
                    key="dataset",
                    match=models.MatchValue(value=dataset),
                )
            )
        if exclude_item_ids:
            must_not.append(
                models.FieldCondition(
                    key="item_id",
                    match=models.MatchAny(any=list(exclude_item_ids)),
                )
            )
        if not must and not must_not:
            return None
        return models.Filter(must=must or None, must_not=must_not or None)

    @staticmethod
    def _payload_to_dict(point: models.ScoredPoint) -> dict[str, Any]:
        payload = point.payload or {}
        return {
            "item_id": payload.get("item_id", ""),
            "name": payload.get("name", ""),
            "dataset": payload.get("dataset", ""),
            "score": float(point.score),
            "metadata": payload.get("metadata", {}),
        }


vector_store = HackathonVectorStore()
