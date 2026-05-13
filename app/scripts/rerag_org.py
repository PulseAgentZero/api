"""Re-embed an org's Qdrant collection with a target embedding model.

Idempotent: skips points already tagged with the target model_version. Re-uses
the same point IDs so partial runs don't duplicate. Reconstructs embedding text
from the payload (`profile_summary` + `behavioural_metrics` + `base_attributes`),
which is exactly what `embed_and_store_profiles` originally wrote.

Usage:
    python -m app.scripts.rerag_org --org <uuid> [--model voyage-4-large] \
        [--batch 64] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.config.settings import settings
from app.infrastructure.external_services.embeddings import (
    EmbeddingService,
    embedding_service,
)
from app.infrastructure.external_services.qdrant import QdrantService
from app.infrastructure.external_services.rag import _profile_to_text

logger = logging.getLogger("rerag_org")


def _payload_to_profile(entity_id: str, payload: dict) -> dict:
    """Reverse a Qdrant payload back into the dict shape `_profile_to_text` expects."""
    return {
        "entity_id": entity_id,
        "profile_summary": payload.get("profile_summary", ""),
        "behavioural_metrics": payload.get("behavioural_metrics", {}),
        "base_attributes": payload.get("base_attributes", {}),
    }


async def _scroll_all_points(qd: QdrantService, collection: str, batch: int):
    """Yield Qdrant points one batch at a time."""
    client = await qd._get_client()
    offset = None
    while True:
        points, offset = await client.scroll(
            collection_name=collection,
            limit=batch,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        if not points:
            return
        yield points
        if offset is None:
            return


async def rerag(
    org_id: str,
    target_model: str,
    *,
    batch: int = 64,
    dry_run: bool = False,
) -> dict[str, int]:
    """Re-embed every point in the org collection with `target_model`.

    Returns a counters dict.
    """
    if not settings.is_voyage_configured():
        raise RuntimeError("VOYAGEAI_API_KEY not configured")

    target_svc = EmbeddingService()
    target_svc.model = target_model  # override singleton for this run
    if not target_svc._ensure_initialized():
        raise RuntimeError("Voyage init failed")

    qd = QdrantService()
    collection = settings.get_org_collection_name(org_id)
    client = await qd._get_client()
    if not await client.collection_exists(collection):
        raise RuntimeError(f"Collection {collection} does not exist")

    seen = 0
    skipped = 0
    re_embedded = 0

    async for points in _scroll_all_points(qd, collection, batch):
        to_embed: list[tuple[str, str, dict]] = []
        for p in points:
            seen += 1
            payload = p.payload or {}
            current_model = payload.get("model_version")
            if current_model == target_model:
                skipped += 1
                continue
            entity_id = payload.get("_entity_id") or str(p.id)
            text = _profile_to_text(_payload_to_profile(entity_id, payload))
            to_embed.append((entity_id, text, payload))

        if not to_embed:
            continue

        texts = [t for _, t, _ in to_embed]
        if dry_run:
            logger.info("DRY-RUN: would re-embed %d points", len(to_embed))
            re_embedded += len(to_embed)
            continue

        vectors = await target_svc.embed_batch(texts, input_type="document")
        items = []
        for (entity_id, _text, payload), vector in zip(to_embed, vectors):
            new_payload = dict(payload)
            new_payload["model_version"] = target_model
            # Preserve `embedded_at` if present; the rotation isn't a fresh
            # profiling cycle, so we don't bump freshness.
            items.append((entity_id, vector, new_payload))
        await qd.upsert_batch(org_id, items)
        re_embedded += len(items)

    counters = {"seen": seen, "skipped": skipped, "re_embedded": re_embedded}
    logger.info("[rerag] %s -> %s : %s", collection, target_model, counters)
    return counters


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True, help="Org UUID")
    parser.add_argument(
        "--model",
        default=embedding_service.model,
        help=f"Target embedding model (default: {embedding_service.model})",
    )
    parser.add_argument("--batch", type=int, default=64)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s  %(message)s",
    )
    counters = asyncio.run(
        rerag(args.org, args.model, batch=args.batch, dry_run=args.dry_run)
    )
    print(counters)
    return 0


if __name__ == "__main__":
    sys.exit(main())
