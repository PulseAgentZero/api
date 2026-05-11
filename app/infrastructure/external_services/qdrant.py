from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from qdrant_client import AsyncQdrantClient, models

from app.config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    entity_id: str
    score: float
    payload: dict[str, Any]


_POINT_ID_NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # DNS namespace


def _to_point_id(entity_id: str) -> str:
    return str(uuid.uuid5(_POINT_ID_NS, entity_id))


class QdrantService:
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
        logger.info("Created Qdrant collection %s for org %s", collection_name, org_id)

    async def upsert_entity(
        self,
        org_id: str,
        entity_id: str,
        vector: list[float],
        payload: dict[str, Any],
    ) -> None:
        client = await self._get_client()
        collection_name = settings.get_org_collection_name(org_id)
        payload["_entity_id"] = entity_id
        await client.upsert(
            collection_name=collection_name,
            points=[
                models.PointStruct(
                    id=_to_point_id(entity_id),
                    vector=vector,
                    payload=payload,
                )
            ],
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
        results = await client.query_points(
            collection_name=collection_name,
            query=vector,
            query_filter=filter_condition,
            limit=limit,
            score_threshold=score_threshold,
            with_payload=True,
        )
        return [
            SearchResult(
                entity_id=(p.payload or {}).get("_entity_id", str(p.id)),
                score=p.score,
                payload=p.payload or {},
            )
            for p in results.points
        ]

    async def remove_entity(self, org_id: str, entity_id: str) -> None:
        client = await self._get_client()
        collection_name = settings.get_org_collection_name(org_id)
        await client.delete(
            collection_name=collection_name,
            points_selector=[_to_point_id(entity_id)],
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
