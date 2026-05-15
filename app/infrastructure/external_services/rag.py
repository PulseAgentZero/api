"""RAG helpers: embed, retrieve, rerank, fuse."""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from dataclasses import dataclass
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


@dataclass
class RagRunStats:
    """Accumulated latency and quality counters for one enrich_entities_with_similar call."""

    entities_enriched: int = 0
    total_rewrite_ms: float = 0.0
    total_embed_ms: float = 0.0
    total_search_ms: float = 0.0
    total_rerank_ms: float = 0.0
    total_rag_ms: float = 0.0
    rerank_applied_count: int = 0
    autocut_removed_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _merge_rag_stats(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Sum numeric fields from two serialised RagRunStats dicts."""
    keys = (
        "entities_enriched", "total_rewrite_ms", "total_embed_ms",
        "total_search_ms", "total_rerank_ms", "total_rag_ms",
        "rerank_applied_count", "autocut_removed_count",
    )
    return {k: a.get(k, 0) + b.get(k, 0) for k in keys}


@dataclass(frozen=True)
class RagConfig:
    """Effective RAG tuning for one request. Built by `RagConfig.resolve`."""

    top_k: int
    prefetch_limit: int
    score_threshold: float
    freshness_days: int
    enable_rerank: bool
    enable_hybrid: bool
    enable_query_rewrite: bool
    enable_autocut: bool = True
    rerank_model: str | None = None

    @classmethod
    def from_defaults(cls) -> "RagConfig":
        return cls(
            top_k=settings.RAG_TOP_K,
            prefetch_limit=settings.RAG_PREFETCH_K,
            score_threshold=settings.RAG_SCORE_THRESHOLD,
            freshness_days=settings.RAG_FRESHNESS_WINDOW_DAYS,
            enable_rerank=settings.RAG_ENABLE_RERANK,
            enable_hybrid=settings.RAG_ENABLE_HYBRID,
            enable_query_rewrite=settings.RAG_ENABLE_QUERY_REWRITE,
            enable_autocut=settings.RAG_ENABLE_AUTOCUT,
            rerank_model=None,
        )

    @classmethod
    def resolve(cls, overrides: dict[str, Any] | None) -> "RagConfig":
        """Layer per-org JSONB overrides on top of settings defaults."""
        base = cls.from_defaults()
        if not overrides:
            return base
        return cls(
            top_k=int(overrides.get("top_k", base.top_k)),
            prefetch_limit=int(overrides.get("prefetch_limit", base.prefetch_limit)),
            score_threshold=float(
                overrides.get("score_threshold", base.score_threshold)
            ),
            freshness_days=int(overrides.get("freshness_days", base.freshness_days)),
            enable_rerank=bool(overrides.get("enable_rerank", base.enable_rerank)),
            enable_hybrid=bool(overrides.get("enable_hybrid", base.enable_hybrid)),
            enable_query_rewrite=bool(
                overrides.get("enable_query_rewrite", base.enable_query_rewrite)
            ),
            enable_autocut=bool(overrides.get("enable_autocut", base.enable_autocut)),
            rerank_model=overrides.get("rerank_model") or base.rerank_model,
        )


def _autocut_candidates(candidates: list[SearchResult]) -> list[SearchResult]:
    """Remove candidates below a significant score gap (Weaviate Autocut).

    Cuts at the first gap that is both >2x the mean gap and >0.05 absolute.
    """
    if len(candidates) <= 1:
        return candidates
    scores = [c.score for c in candidates if c.score is not None]
    if len(scores) < 2:
        return candidates
    gaps = [scores[i] - scores[i + 1] for i in range(len(scores) - 1)]
    mean_gap = sum(gaps) / len(gaps)
    for i, gap in enumerate(gaps):
        if gap > 2.0 * mean_gap and gap > 0.05:
            return candidates[: i + 1]
    return candidates


async def run_ttl_cleanup(org_id: str, ttl_days: int | None = None) -> int:
    """Delete Qdrant points older than ttl_days. Returns count removed."""
    if not settings.is_qdrant_configured():
        return 0
    effective_days = ttl_days if ttl_days is not None else settings.QDRANT_TTL_DAYS
    qdrant = QdrantService()
    return await qdrant.archive_stale_points(org_id, effective_days)


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
    config: RagConfig,
    filter_condition: qmodels.Filter | None,
) -> list[SearchResult]:
    """Dense or hybrid retrieval of over-fetched candidates."""
    vector = await svc.embed_query(rewritten_query or query_text)
    if config.enable_hybrid:
        return await qdrant.hybrid_search(
            org_id,
            vector,
            text_query=rewritten_query or query_text,
            limit=config.prefetch_limit,
            prefetch_limit=config.prefetch_limit,
            filter_condition=filter_condition,
        )
    return await qdrant.search_similar(
        org_id,
        vector,
        limit=config.prefetch_limit,
        score_threshold=config.score_threshold,
        filter_condition=filter_condition,
    )


async def enrich_entities_with_similar(
    org_id: str,
    entities: list[dict],
    *,
    embedding_svc: EmbeddingService | None = None,
    qdrant: QdrantService | None = None,
    reranker: VoyageReranker | None = None,
    config: RagConfig | None = None,
    limit: int | None = None,
    prefetch_limit: int | None = None,
    score_threshold: float | None = None,
    past_recs_by_entity: dict[str, list[dict]] | None = None,
    tier_filter: str | None = None,
    run_stats: RagRunStats | None = None,
) -> list[dict]:
    """Attach `similar_entities` per entity using the full RAG pipeline.

    Pipeline per entity (each stage gracefully degrades on failure):
      1. Build raw query text from profile + signals
      2. (optional) LLM rewrite into a focused retrieval query
      3. Embed query with Voyage
      4. (optional) Hybrid dense+keyword retrieval with RRF, over-fetched
      5. Apply metadata filter (risk_tier hint, freshness window)
      5b. (optional) Autocut: remove candidates below a significant score gap
      6. (optional) Voyage rerank to top-K
      7. Self-exclude and attach past recommendations if provided

    `config` carries effective tuning (per-org overrides resolved by caller).
    `limit`/`prefetch_limit`/`score_threshold` remain as ad-hoc overrides for
    tests and one-off callers. `run_stats` collects per-call latency telemetry.
    """
    if not entities or not settings.is_voyage_configured():
        return entities

    svc = embedding_svc or embedding_service
    qd = qdrant or QdrantService()
    cfg = config or RagConfig.from_defaults()
    if limit is not None or prefetch_limit is not None or score_threshold is not None:
        cfg = RagConfig(
            top_k=limit if limit is not None else cfg.top_k,
            prefetch_limit=(
                prefetch_limit if prefetch_limit is not None else cfg.prefetch_limit
            ),
            score_threshold=(
                score_threshold if score_threshold is not None else cfg.score_threshold
            ),
            freshness_days=cfg.freshness_days,
            enable_rerank=cfg.enable_rerank,
            enable_hybrid=cfg.enable_hybrid,
            enable_query_rewrite=cfg.enable_query_rewrite,
            enable_autocut=cfg.enable_autocut,
            rerank_model=cfg.rerank_model,
        )
    rerank = reranker or (
        VoyageReranker(model=cfg.rerank_model) if cfg.rerank_model else voyage_reranker
    )

    try:
        await qd.ensure_collection(org_id)
    except Exception as exc:
        logger.warning("[RAG] ensure_collection failed: %s", exc)
        return entities

    filter_condition = _build_filter(
        risk_tier=tier_filter,
        freshness_days=cfg.freshness_days,
    )

    enriched: list[dict] = []
    wall_t0 = time.perf_counter()

    for entity in entities:
        eid = entity.get("entity_id")
        raw_query = _entity_to_query_text(entity)
        if not raw_query:
            enriched.append(entity)
            continue

        if cfg.enable_query_rewrite:
            t_rw = time.perf_counter()
            try:
                rewritten = await rewrite_entity_query(entity, raw_query)
            except Exception as exc:
                logger.debug("[RAG] query rewrite failed (using raw): %s", exc)
                rewritten = raw_query
            if run_stats is not None:
                run_stats.total_rewrite_ms += (time.perf_counter() - t_rw) * 1000
        else:
            rewritten = raw_query

        t_search = time.perf_counter()
        try:
            candidates = await _retrieve_candidates(
                org_id,
                query_text=raw_query,
                rewritten_query=rewritten,
                qdrant=qd,
                svc=svc,
                config=RagConfig(
                    top_k=cfg.top_k,
                    prefetch_limit=max(cfg.prefetch_limit, cfg.top_k + 1),
                    score_threshold=cfg.score_threshold,
                    freshness_days=cfg.freshness_days,
                    enable_rerank=cfg.enable_rerank,
                    enable_hybrid=cfg.enable_hybrid,
                    enable_query_rewrite=cfg.enable_query_rewrite,
                    enable_autocut=cfg.enable_autocut,
                    rerank_model=cfg.rerank_model,
                ),
                filter_condition=filter_condition,
            )
        except Exception as exc:
            logger.warning("[RAG] retrieval failed for entity %s: %s", eid, exc)
            enriched.append(entity)
            continue
        if run_stats is not None:
            run_stats.total_search_ms += (time.perf_counter() - t_search) * 1000

        # Self-exclusion
        candidates = [c for c in candidates if str(c.entity_id) != str(eid)]

        # Distance threshold (post-filter; hybrid path doesn't take it natively)
        filtered = [
            c for c in candidates if c.score is None or c.score >= cfg.score_threshold
        ]
        if not filtered and candidates:
            # Keep the top dense candidate if everything got filtered — better
            # than nothing if the threshold is too aggressive.
            filtered = candidates[: max(cfg.top_k, 1)]

        # Autocut: remove candidates below a significant score gap (Weaviate ebook)
        if cfg.enable_autocut and len(filtered) > 1:
            before_autocut = len(filtered)
            filtered = _autocut_candidates(filtered)
            if run_stats is not None:
                run_stats.autocut_removed_count += before_autocut - len(filtered)

        # Rerank with Voyage rerank model over the over-fetched set
        if cfg.enable_rerank and len(filtered) > cfg.top_k:
            t_rr = time.perf_counter()
            try:
                docs = [
                    c.payload.get("profile_summary") or _profile_to_text(c.payload)
                    for c in filtered
                ]
                order = await rerank.rerank(
                    query=rewritten,
                    documents=docs,
                    top_n=cfg.top_k,
                )
                filtered = [filtered[i] for i, _ in order]
                if run_stats is not None:
                    run_stats.rerank_applied_count += 1
                    run_stats.total_rerank_ms += (time.perf_counter() - t_rr) * 1000
            except Exception as exc:
                logger.debug("[RAG] rerank failed (keeping dense order): %s", exc)
                filtered = filtered[: cfg.top_k]
        else:
            filtered = filtered[: cfg.top_k]

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
        if run_stats is not None:
            run_stats.entities_enriched += 1

    if run_stats is not None:
        run_stats.total_rag_ms = (time.perf_counter() - wall_t0) * 1000

    return enriched
