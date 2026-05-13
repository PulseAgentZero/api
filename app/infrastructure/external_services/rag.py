"""Production-grade RAG helpers: embed, retrieve, rerank, fuse."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from qdrant_client import models as qmodels

from app.config.settings import settings
from app.infrastructure.external_services.embeddings import (
    EmbeddingError,
    EmbeddingService,
    embedding_service,
)
from app.infrastructure.external_services.qdrant import QdrantService, SearchResult
from app.infrastructure.external_services.reranker import (
    VoyageReranker,
    voyage_reranker,
)
from app.infrastructure.external_services.query_rewrite import rewrite_entity_query

logger = logging.getLogger(__name__)


def _profile_to_text(profile: dict) -> str:
    """Serialize a profile dict into a single embedding-ready string."""
    parts: list[str] = [f"Entity {profile.get('entity_id', 'unknown')}"]
    summary = profile.get("profile_summary", "")
    if summary:
        parts.append(str(summary))
    metrics = profile.get("behavioural_metrics", {})
    if metrics:
        parts.append(f"Metrics: {json.dumps(metrics, default=str)}")
    attrs = profile.get("base_attributes", {})
    if attrs:
        parts.append(f"Attributes: {json.dumps(attrs, default=str)}")
    return " | ".join(parts)


def _entity_to_query_text(entity: dict) -> str:
    """Concise dump of an entity's profile + signals for retrieval."""
    parts: list[str] = []
    profile = entity.get("profile") or {}
    if profile:
        summary = profile.get("profile_summary", "")
        if summary:
            parts.append(str(summary))
        metrics = profile.get("behavioural_metrics", {})
        if metrics:
            parts.append(f"behaviour: {json.dumps(metrics, default=str)}")
    signals = entity.get("signal_values") or {}
    if signals:
        parts.append(f"signals: {json.dumps(signals, default=str)}")
    return " | ".join(parts)


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


async def embed_and_store_profiles(org_id: str, profiles: list[dict]) -> None:
    """Embed entity profiles and batch-upsert with model + freshness metadata."""
    if not profiles or not settings.is_voyage_configured():
        return

    try:
        qdrant = QdrantService()
        await qdrant.ensure_collection(org_id)

        texts: list[str] = []
        entity_ids: list[str] = []
        for p in profiles:
            eid = p.get("entity_id")
            if eid is None:
                continue
            texts.append(_profile_to_text(p))
            entity_ids.append(str(eid))

        if not texts:
            return

        t0 = time.perf_counter()
        vectors = await embedding_service.embed_batch(texts, input_type="document")
        embed_ms = (time.perf_counter() - t0) * 1000

        profile_by_id = {str(p.get("entity_id")): p for p in profiles}
        ts = _now_ts()
        items: list[tuple[str, list[float], dict[str, Any]]] = []
        for entity_id, vector in zip(entity_ids, vectors):
            profile = profile_by_id.get(entity_id, {})
            payload: dict[str, Any] = {
                "profile_summary": str(profile.get("profile_summary", "")),
                "behavioural_metrics": profile.get("behavioural_metrics", {}),
                "base_attributes": profile.get("base_attributes", {}),
                "model_version": embedding_service.model,
                "embedded_at": ts,
            }
            items.append((entity_id, vector, payload))

        await qdrant.upsert_batch(org_id, items)
        logger.info(
            "[RAG] upserted %d profiles for org=%s (embed=%.0fms model=%s)",
            len(items),
            org_id,
            embed_ms,
            embedding_service.model,
        )
    except (EmbeddingError, Exception) as exc:
        logger.warning("[RAG] embed_and_store_profiles failed (non-fatal): %s", exc)


async def update_entity_metadata(
    org_id: str, updates: list[tuple[str, dict[str, Any]]]
) -> None:
    """Patch Qdrant payloads after scoring with risk_tier / last_scored_at etc."""
    if not updates or not settings.is_voyage_configured():
        return
    try:
        qdrant = QdrantService()
        await qdrant.set_payload_batch(org_id, updates)
    except Exception as exc:
        logger.warning("[RAG] update_entity_metadata failed (non-fatal): %s", exc)


def _build_filter(
    *,
    risk_tier: str | None = None,
    freshness_days: int | None = None,
    model_version: str | None = None,
) -> qmodels.Filter | None:
    """Build a Qdrant filter from optional metadata constraints."""
    must: list[qmodels.FieldCondition] = []
    if risk_tier:
        must.append(
            qmodels.FieldCondition(
                key="risk_tier",
                match=qmodels.MatchValue(value=risk_tier),
            )
        )
    if model_version:
        must.append(
            qmodels.FieldCondition(
                key="model_version",
                match=qmodels.MatchValue(value=model_version),
            )
        )
    if freshness_days is not None and freshness_days > 0:
        cutoff = _now_ts() - (freshness_days * 86400)
        must.append(
            qmodels.FieldCondition(
                key="embedded_at",
                range=qmodels.Range(gte=cutoff),
            )
        )
    if not must:
        return None
    return qmodels.Filter(must=must)


async def _retrieve_candidates(
    org_id: str,
    *,
    query_text: str,
    rewritten_query: str,
    qdrant: QdrantService,
    svc: EmbeddingService,
    prefetch_limit: int,
    filter_condition: qmodels.Filter | None,
) -> list[SearchResult]:
    """Dense or hybrid retrieval of over-fetched candidates."""
    vector = await svc.embed_query(rewritten_query or query_text)
    if settings.RAG_ENABLE_HYBRID:
        return await qdrant.hybrid_search(
            org_id,
            vector,
            text_query=rewritten_query or query_text,
            limit=prefetch_limit,
            prefetch_limit=prefetch_limit,
            filter_condition=filter_condition,
        )
    return await qdrant.search_similar(
        org_id,
        vector,
        limit=prefetch_limit,
        score_threshold=settings.RAG_SCORE_THRESHOLD,
        filter_condition=filter_condition,
    )


async def enrich_entities_with_similar(
    org_id: str,
    entities: list[dict],
    *,
    embedding_svc: EmbeddingService | None = None,
    qdrant: QdrantService | None = None,
    reranker: VoyageReranker | None = None,
    limit: int | None = None,
    prefetch_limit: int | None = None,
    score_threshold: float | None = None,
    past_recs_by_entity: dict[str, list[dict]] | None = None,
    tier_filter: str | None = None,
) -> list[dict]:
    """Attach `similar_entities` per entity using the full RAG pipeline.

    Pipeline per entity (each stage gracefully degrades on failure):
      1. Build raw query text from profile + signals
      2. (optional) LLM rewrite into a focused retrieval query
      3. Embed query with Voyage
      4. (optional) Hybrid dense+keyword retrieval with RRF, over-fetched
      5. Apply metadata filter (risk_tier hint, freshness window)
      6. (optional) Voyage rerank-2-lite to top-K
      7. Self-exclude and attach past recommendations if provided
    """
    if not entities or not settings.is_voyage_configured():
        return entities

    svc = embedding_svc or embedding_service
    qd = qdrant or QdrantService()
    rerank = reranker or voyage_reranker
    top_k = limit if limit is not None else settings.RAG_TOP_K
    over_k = prefetch_limit if prefetch_limit is not None else settings.RAG_PREFETCH_K
    threshold = (
        score_threshold if score_threshold is not None else settings.RAG_SCORE_THRESHOLD
    )

    try:
        await qd.ensure_collection(org_id)
    except Exception as exc:
        logger.warning("[RAG] ensure_collection failed: %s", exc)
        return entities

    filter_condition = _build_filter(
        risk_tier=tier_filter,
        freshness_days=settings.RAG_FRESHNESS_WINDOW_DAYS,
    )

    enriched: list[dict] = []
    for entity in entities:
        eid = entity.get("entity_id")
        raw_query = _entity_to_query_text(entity)
        if not raw_query:
            enriched.append(entity)
            continue

        try:
            rewritten = await rewrite_entity_query(entity, raw_query)
        except Exception as exc:
            logger.debug("[RAG] query rewrite failed (using raw): %s", exc)
            rewritten = raw_query

        try:
            candidates = await _retrieve_candidates(
                org_id,
                query_text=raw_query,
                rewritten_query=rewritten,
                qdrant=qd,
                svc=svc,
                prefetch_limit=max(over_k, top_k + 1),
                filter_condition=filter_condition,
            )
        except Exception as exc:
            logger.warning("[RAG] retrieval failed for entity %s: %s", eid, exc)
            enriched.append(entity)
            continue

        # Self-exclusion
        candidates = [c for c in candidates if str(c.entity_id) != str(eid)]

        # Distance threshold (post-filter; hybrid path doesn't take it natively)
        filtered = [c for c in candidates if c.score is None or c.score >= threshold]
        if not filtered and candidates:
            # Keep the top dense candidate if everything got filtered — better
            # than nothing if the threshold is too aggressive.
            filtered = candidates[: max(top_k, 1)]

        # Rerank with Voyage rerank-2-lite over the over-fetched set
        if settings.RAG_ENABLE_RERANK and len(filtered) > top_k:
            try:
                docs = [
                    c.payload.get("profile_summary") or _profile_to_text(c.payload)
                    for c in filtered
                ]
                order = await rerank.rerank(
                    query=rewritten,
                    documents=docs,
                    top_n=top_k,
                )
                filtered = [filtered[i] for i, _ in order]
            except Exception as exc:
                logger.debug("[RAG] rerank failed (keeping dense order): %s", exc)
                filtered = filtered[:top_k]
        else:
            filtered = filtered[:top_k]

        similar: list[dict] = []
        for r in filtered:
            item = {
                "entity_id": r.entity_id,
                "similarity": round(float(r.score or 0.0), 4),
                "profile_summary": r.payload.get("profile_summary", ""),
                "behavioural_metrics": r.payload.get("behavioural_metrics", {}),
                "risk_tier": r.payload.get("risk_tier"),
                "model_version": r.payload.get("model_version"),
            }
            if past_recs_by_entity is not None:
                item["past_recommendations"] = past_recs_by_entity.get(
                    str(r.entity_id), []
                )
            similar.append(item)

        entity_copy = dict(entity)
        entity_copy["similar_entities"] = similar
        enriched.append(entity_copy)

    return enriched
