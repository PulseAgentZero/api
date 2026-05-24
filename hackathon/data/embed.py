"""Batch-embed every loaded item and precompute mean user vectors.

Item embeddings live in Qdrant (used for ANN retrieval).
User vectors are persisted on ``users.persona_meta.user_vector`` so the
recommender can score warm-start users without recomputing on every request.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from hackathon.core.db import upsert_user
from hackathon.core.embeddings import embed_documents
from hackathon.core.vector_store import vector_store

logger = logging.getLogger(__name__)


def _item_text(row: dict[str, Any]) -> str:
    meta = row.get("metadata") or {}
    parts = [
        row.get("name", ""),
        str(meta.get("categories", "")),
        str(meta.get("genres", "")),
    ]
    return " | ".join(p for p in parts if p)


async def _load_items(session: AsyncSession) -> list[dict[str, Any]]:
    result = await session.execute(
        text("SELECT id, dataset, name, metadata FROM items ORDER BY dataset, id")
    )
    return [dict(r._mapping) for r in result]


async def _upsert_item_vectors(
    items: list[dict[str, Any]],
    vectors: list[list[float]],
) -> None:
    rows = [
        (
            item["id"],
            vector,
            {
                "item_id": item["id"],
                "dataset": item["dataset"],
                "name": item["name"],
                "metadata": item["metadata"],
            },
        )
        for item, vector in zip(items, vectors)
    ]
    await vector_store.upsert_items(rows)


async def _persist_user_vectors(
    session: AsyncSession,
    item_vectors: dict[str, list[float]],
) -> int:
    """Average each user's training-set item vectors and persist into persona_meta."""
    result = await session.execute(
        text(
            """
            SELECT u.id AS user_id,
                   u.dataset,
                   u.persona_meta,
                   array_agg(DISTINCT r.item_id) AS item_ids
            FROM users u
            JOIN reviews r ON r.user_id = u.id AND r.is_holdout = FALSE
            GROUP BY u.id
            """
        )
    )
    n_updated = 0
    for row in result:
        m = row._mapping
        vectors = [item_vectors[iid] for iid in (m["item_ids"] or []) if iid in item_vectors]
        if not vectors:
            continue
        dim = len(vectors[0])
        mean_vec = [sum(v[d] for v in vectors) / len(vectors) for d in range(dim)]
        meta = dict(m["persona_meta"] or {})
        meta["user_vector"] = mean_vec
        await upsert_user(session, m["user_id"], m["dataset"], meta)
        n_updated += 1
    return n_updated


async def embed_all_items(session: AsyncSession) -> int:
    """Embed every item in Postgres, push to Qdrant, then cache user vectors."""
    await vector_store.ensure_collection()
    items = await _load_items(session)
    if not items:
        logger.info("No items to embed")
        return 0

    logger.info("Embedding %d items", len(items))
    vectors = embed_documents([_item_text(i) for i in items])
    await _upsert_item_vectors(items, vectors)

    item_vectors = {item["id"]: vec for item, vec in zip(items, vectors)}
    n_users = await _persist_user_vectors(session, item_vectors)
    logger.info("Embedded %d items, cached %d user vectors", len(items), n_users)
    return len(items)
