"""RAG helpers: embed, retrieve, rerank, fuse."""

from __future__ import annotations

import asyncio
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
from app.infrastructure.external_services.query_rewrite import (
    expand_query,
    rewrite_entity_query,
    validate_retrieval_relevance,
)

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
    validation_applied_count: int = 0
    validation_rejected_count: int = 0
    expansion_applied_count: int = 0
    expansion_extra_candidates: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _merge_rag_stats(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Sum numeric fields from two serialised RagRunStats dicts."""
    keys = (
        "entities_enriched", "total_rewrite_ms", "total_embed_ms",
        "total_search_ms", "total_rerank_ms", "total_rag_ms",
        "rerank_applied_count", "autocut_removed_count",
        "validation_applied_count", "validation_rejected_count",
        "expansion_applied_count", "expansion_extra_candidates",
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
    enable_hierarchical_chunks: bool = True
    enable_retrieval_validation: bool = False
    enable_query_expansion: bool = False

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
            enable_hierarchical_chunks=settings.RAG_ENABLE_HIERARCHICAL_CHUNKS,
            enable_retrieval_validation=settings.RAG_ENABLE_RETRIEVAL_VALIDATION,
            enable_query_expansion=settings.RAG_ENABLE_QUERY_EXPANSION,
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
            enable_hierarchical_chunks=bool(
                overrides.get("enable_hierarchical_chunks", base.enable_hierarchical_chunks)
            ),
            enable_retrieval_validation=bool(
                overrides.get("enable_retrieval_validation", base.enable_retrieval_validation)
            ),
            enable_query_expansion=bool(
                overrides.get("enable_query_expansion", base.enable_query_expansion)
            ),
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


def _profile_to_summary_text(profile: dict) -> str:
    """Compact (~100-150 token) embedding anchor focused on dominant signals.

    Avoids the "Rich but Unfindable" problem — full-narrative embeddings are too
    diffuse for precise cosine similarity. Full text is preserved in the payload.
    """
    parts: list[str] = [f"Entity {profile.get('entity_id', 'unknown')}"]

    summary = str(profile.get("profile_summary", "")).strip()
    if summary:
        short = summary[:200]
        for sep in (". ", "! ", "? "):
            idx = summary.find(sep, 40)
            if 40 <= idx <= 200:
                short = summary[: idx + 1]
                break
        parts.append(short)

    metrics = profile.get("behavioural_metrics", {})
    if metrics and isinstance(metrics, dict):
        top = sorted(
            ((k, v) for k, v in metrics.items() if isinstance(v, (int, float))),
            key=lambda kv: abs(kv[1]),
            reverse=True,
        )[:3]
        if top:
            parts.append("signals: " + ", ".join(f"{k}={v}" for k, v in top))

    attrs = profile.get("base_attributes", {})
    if attrs and isinstance(attrs, dict):
        cats = [(k, v) for k, v in attrs.items() if isinstance(v, str)][:2]
        if cats:
            parts.append("attrs: " + ", ".join(f"{k}={v}" for k, v in cats))

    return " | ".join(parts)


def _profile_to_signals_text(profile: dict) -> str:
    """Focused embedding text for signal-specific retrieval (behavioral_signals chunk)."""
    parts: list[str] = [f"Entity {profile.get('entity_id', 'unknown')} signals"]
    metrics = profile.get("behavioural_metrics", {})
    if metrics and isinstance(metrics, dict):
        numeric = sorted(
            ((k, v) for k, v in metrics.items() if isinstance(v, (int, float))),
            key=lambda kv: abs(kv[1]),
            reverse=True,
        )[:15]
        if numeric:
            parts.append(", ".join(f"{k}={v}" for k, v in numeric))
    risk_tier = profile.get("risk_tier") or (profile.get("base_attributes") or {}).get("risk_tier")
    if risk_tier:
        parts.append(f"tier={risk_tier}")
    return " | ".join(parts)


def _profile_to_anomalies_text(profile: dict) -> str:
    """Focused embedding text for delta/outlier retrieval (anomalies chunk)."""
    parts: list[str] = [f"Entity {profile.get('entity_id', 'unknown')} anomalies"]
    metrics = profile.get("behavioural_metrics", {})
    if metrics and isinstance(metrics, dict):
        delta_pairs = [
            (k, v) for k, v in metrics.items()
            if isinstance(v, (int, float)) and any(
                kw in k.lower()
                for kw in ("delta", "change", "diff", "trend", "velocity", "rate", "shift")
            )
        ]
        if delta_pairs:
            delta_pairs.sort(key=lambda kv: abs(kv[1]), reverse=True)
            parts.append("deltas: " + ", ".join(f"{k}={v}" for k, v in delta_pairs[:10]))
        else:
            # Proxy: top absolute-magnitude signals as outlier stand-ins
            top = sorted(
                ((k, v) for k, v in metrics.items() if isinstance(v, (int, float))),
                key=lambda kv: abs(kv[1]),
                reverse=True,
            )[:5]
            if top:
                parts.append("outliers: " + ", ".join(f"{k}={v}" for k, v in top))
    summary = str(profile.get("profile_summary", "")).strip()
    if summary:
        parts.append(summary[:100])
    return " | ".join(parts)


def _entity_to_query_text(entity: dict) -> str:
    """Concise dump of an entity's profile + signals for retrieval."""
    parts: list[str] = []
    profile = entity.get("profile") or {}
    summary = profile.get("profile_summary") or entity.get("profile_summary") or ""
    if summary:
        parts.append(str(summary))
    metrics = profile.get("behavioural_metrics") or entity.get("behavioural_metrics") or {}
    if metrics:
        parts.append(f"behaviour: {json.dumps(metrics, default=str)}")
    signals = entity.get("signal_values") or {}
    if signals:
        parts.append(f"signals: {json.dumps(signals, default=str)}")
    return " | ".join(parts)


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


async def embed_and_store_profiles(org_id: str, profiles: list[dict]) -> None:
    """Embed entity profiles and batch-upsert with model + freshness metadata.

    When RAG_ENABLE_HIERARCHICAL_CHUNKS is true, stores three chunk types per entity:
    summary (broad), behavioral_signals (metrics-focused), anomalies (delta/outlier-focused).
    All chunks share the same entity_id payload field for cross-chunk deduplication.
    """
    if not profiles or not settings.is_voyage_configured():
        return

    try:
        qdrant = QdrantService()
        await qdrant.ensure_collection(org_id)

        use_hierarchical = settings.RAG_ENABLE_HIERARCHICAL_CHUNKS

        # (point_key, embedding_text, real_entity_id, chunk_type)
        chunk_specs: list[tuple[str, str, str, str]] = []
        for p in profiles:
            eid = p.get("entity_id")
            if eid is None:
                continue
            eid_str = str(eid)
            chunk_specs.append((eid_str, _profile_to_summary_text(p), eid_str, "summary"))
            if use_hierarchical:
                chunk_specs.append((f"{eid_str}:bs", _profile_to_signals_text(p), eid_str, "behavioral_signals"))
                chunk_specs.append((f"{eid_str}:an", _profile_to_anomalies_text(p), eid_str, "anomalies"))

        if not chunk_specs:
            return

        t0 = time.perf_counter()
        vectors = await embedding_service.embed_batch(
            [cs[1] for cs in chunk_specs], input_type="document"
        )
        embed_ms = (time.perf_counter() - t0) * 1000

        profile_by_id = {str(p.get("entity_id")): p for p in profiles}
        ts = _now_ts()
        items: list[tuple[str, list[float], dict[str, Any]]] = []
        for (point_key, _, entity_id, chunk_type), vector in zip(chunk_specs, vectors):
            profile = profile_by_id.get(entity_id, {})
            payload: dict[str, Any] = {
                "entity_id": entity_id,
                "profile_summary": str(profile.get("profile_summary", "")),
                "behavioural_metrics": profile.get("behavioural_metrics", {}),
                "base_attributes": profile.get("base_attributes", {}),
                "model_version": embedding_service.model,
                "embedded_at": ts,
                "chunk_type": chunk_type,
            }
            items.append((point_key, vector, payload))

        await qdrant.upsert_batch(org_id, items)
        n_entities = sum(1 for cs in chunk_specs if cs[3] == "summary")
        logger.info(
            "[RAG] upserted %d chunks (%d entities, hierarchical=%s) for org=%s (embed=%.0fms model=%s)",
            len(items),
            n_entities,
            use_hierarchical,
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


async def _retrieve_multi_query(
    org_id: str,
    *,
    query_texts: list[str],
    qdrant: QdrantService,
    svc: EmbeddingService,
    config: RagConfig,
    filter_condition: qmodels.Filter | None,
) -> tuple[list[SearchResult], int]:
    """Batch-embed query variants, search per variant in parallel, merge by entity_id (max score).

    Returns (merged_candidates, max_single_variant_count) so callers can compute the
    expansion lift over the strongest single-query baseline.
    """
    vectors = await svc.embed_batch(query_texts, input_type="query")

    async def _search_one(vec: list[float], txt: str) -> list[SearchResult]:
        if config.enable_hybrid:
            return await qdrant.hybrid_search(
                org_id,
                vec,
                text_query=txt,
                limit=config.prefetch_limit,
                prefetch_limit=config.prefetch_limit,
                filter_condition=filter_condition,
            )
        return await qdrant.search_similar(
            org_id,
            vec,
            limit=config.prefetch_limit,
            score_threshold=config.score_threshold,
            filter_condition=filter_condition,
        )

    per_variant = await asyncio.gather(
        *[_search_one(vec, txt) for vec, txt in zip(vectors, query_texts)],
        return_exceptions=True,
    )

    merged: dict[str, SearchResult] = {}
    max_single = 0
    for results in per_variant:
        if isinstance(results, BaseException):
            continue
        if len(results) > max_single:
            max_single = len(results)
        for r in results:
            eid_str = str(r.entity_id)
            existing = merged.get(eid_str)
            if existing is None or (r.score or 0.0) > (existing.score or 0.0):
                merged[eid_str] = r

    candidates = sorted(merged.values(), key=lambda r: r.score or 0.0, reverse=True)
    return candidates, max_single


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
            enable_hierarchical_chunks=cfg.enable_hierarchical_chunks,
            enable_retrieval_validation=cfg.enable_retrieval_validation,
            enable_query_expansion=cfg.enable_query_expansion,
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

    wall_t0 = time.perf_counter()
    sem = asyncio.Semaphore(settings.RAG_ENRICH_CONCURRENCY)
    stats_lock = asyncio.Lock()

    async def _enrich_one(entity: dict) -> dict:
        async with sem:
            return await _enrich_one_unlocked(entity)

    async def _enrich_one_unlocked(entity: dict) -> dict:
        eid = entity.get("entity_id")
        raw_query = _entity_to_query_text(entity)
        if not raw_query:
            return entity

        if cfg.enable_query_rewrite:
            t_rw = time.perf_counter()
            try:
                rewritten = await rewrite_entity_query(entity, raw_query)
            except Exception as exc:
                logger.debug("[RAG] query rewrite failed (using raw): %s", exc)
                rewritten = raw_query
            if run_stats is not None:
                async with stats_lock:
                    run_stats.total_rewrite_ms += (time.perf_counter() - t_rw) * 1000
        else:
            rewritten = raw_query

        # Build query variants. Skip expansion on short queries — variants drift
        # too easily from already-terse text (Context Engineering, Query Augmentation).
        variants: list[str] = [rewritten]
        if cfg.enable_query_expansion and len(rewritten.split()) >= 5:
            try:
                expanded = await expand_query(rewritten)
                # Dedup, preserve order, cap at 4 (original + up to 3 variants).
                seen_v: set[str] = set()
                variants = []
                for v in expanded:
                    v_norm = v.strip()
                    if v_norm and v_norm.lower() not in seen_v:
                        seen_v.add(v_norm.lower())
                        variants.append(v_norm)
                    if len(variants) >= 4:
                        break
                if not variants:
                    variants = [rewritten]
            except Exception as exc:
                logger.debug("[RAG] query expansion failed (single query): %s", exc)
                variants = [rewritten]

        retrieval_cfg = RagConfig(
            top_k=cfg.top_k,
            prefetch_limit=max(cfg.prefetch_limit, cfg.top_k + 1),
            score_threshold=cfg.score_threshold,
            freshness_days=cfg.freshness_days,
            enable_rerank=cfg.enable_rerank,
            enable_hybrid=cfg.enable_hybrid,
            enable_query_rewrite=cfg.enable_query_rewrite,
            enable_autocut=cfg.enable_autocut,
            rerank_model=cfg.rerank_model,
        )

        t_search = time.perf_counter()
        try:
            if len(variants) <= 1:
                candidates = await _retrieve_candidates(
                    org_id,
                    query_text=raw_query,
                    rewritten_query=variants[0],
                    qdrant=qd,
                    svc=svc,
                    config=retrieval_cfg,
                    filter_condition=filter_condition,
                )
            else:
                candidates, max_single = await _retrieve_multi_query(
                    org_id,
                    query_texts=variants,
                    qdrant=qd,
                    svc=svc,
                    config=retrieval_cfg,
                    filter_condition=filter_condition,
                )
                if run_stats is not None:
                    async with stats_lock:
                        run_stats.expansion_applied_count += 1
                        run_stats.expansion_extra_candidates += max(
                            0, len(candidates) - max_single
                        )
        except Exception as exc:
            logger.warning("[RAG] retrieval failed for entity %s: %s", eid, exc)
            return entity
        if run_stats is not None:
            async with stats_lock:
                run_stats.total_search_ms += (time.perf_counter() - t_search) * 1000

        # Self-exclusion
        candidates = [c for c in candidates if str(c.entity_id) != str(eid)]

        # Dedup: hierarchical storage can return multiple chunks per entity;
        # keep the highest-scoring chunk per entity_id.
        if len(candidates) > 1:
            seen: dict[str, SearchResult] = {}
            for c in candidates:
                c_eid = str(c.entity_id)
                if c_eid not in seen or (c.score or 0.0) > (seen[c_eid].score or 0.0):
                    seen[c_eid] = c
            if len(seen) < len(candidates):
                candidates = sorted(seen.values(), key=lambda r: r.score or 0.0, reverse=True)

        # Distance threshold (post-filter; hybrid path doesn't take it natively)
        filtered = [
            c for c in candidates if c.score is None or c.score >= cfg.score_threshold
        ]
        if not filtered and candidates:
            # Adaptive threshold fallback: retry with a looser threshold rather than
            # blindly keeping the top candidate, which may be irrelevant.
            fallback_threshold = cfg.score_threshold * settings.RAG_THRESHOLD_FALLBACK_FACTOR
            logger.debug(
                "[RAG] entity=%s: no candidates above %.2f; retrying at %.2f",
                eid, cfg.score_threshold, fallback_threshold,
            )
            try:
                retry_candidates = await _retrieve_candidates(
                    org_id,
                    query_text=raw_query,
                    rewritten_query=rewritten,
                    qdrant=qd,
                    svc=svc,
                    config=RagConfig(
                        top_k=cfg.top_k,
                        prefetch_limit=max(cfg.prefetch_limit, cfg.top_k + 1),
                        score_threshold=fallback_threshold,
                        freshness_days=cfg.freshness_days,
                        enable_rerank=cfg.enable_rerank,
                        enable_hybrid=False,  # pure dense for fallback — more stable
                        enable_query_rewrite=False,
                        enable_autocut=cfg.enable_autocut,
                        rerank_model=cfg.rerank_model,
                    ),
                    filter_condition=filter_condition,
                )
                retry_candidates = [c for c in retry_candidates if str(c.entity_id) != str(eid)]
                filtered = [c for c in retry_candidates if c.score is None or c.score >= fallback_threshold]
                if not filtered:
                    filtered = retry_candidates[: max(cfg.top_k, 1)]
                logger.debug("[RAG] entity=%s: fallback retrieved %d candidates", eid, len(filtered))
            except Exception as exc:
                logger.debug("[RAG] threshold fallback failed for %s: %s", eid, exc)
                filtered = candidates[: max(cfg.top_k, 1)]

        # Autocut: remove candidates below a significant score gap (Weaviate ebook)
        if cfg.enable_autocut and len(filtered) > 1:
            before_autocut = len(filtered)
            filtered = _autocut_candidates(filtered)
            if run_stats is not None:
                async with stats_lock:
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
                    async with stats_lock:
                        run_stats.rerank_applied_count += 1
                        run_stats.total_rerank_ms += (time.perf_counter() - t_rr) * 1000
            except Exception as exc:
                logger.debug("[RAG] rerank failed (keeping dense order): %s", exc)
                filtered = filtered[: cfg.top_k]
        else:
            filtered = filtered[: cfg.top_k]

        # (optional) Post-rerank semantic relevance validation — quality gate before
        # results enter LLM context. Degrades gracefully: if fewer than
        # RAG_VALIDATION_MIN_RELEVANT pass, the pre-validation set is kept.
        if cfg.enable_retrieval_validation and filtered:
            try:
                summaries = [
                    r.payload.get("profile_summary") or _profile_to_text(r.payload)
                    for r in filtered
                ]
                valid_indices = await validate_retrieval_relevance(rewritten, summaries)
                if (
                    valid_indices is not None
                    and len(valid_indices) >= settings.RAG_VALIDATION_MIN_RELEVANT
                ):
                    rejected = len(filtered) - len(valid_indices)
                    filtered = [filtered[i] for i in valid_indices]
                    if run_stats is not None:
                        async with stats_lock:
                            run_stats.validation_applied_count += 1
                            run_stats.validation_rejected_count += rejected
                    logger.debug(
                        "[RAG] entity=%s: validation kept %d, rejected %d",
                        eid, len(filtered), rejected,
                    )
            except Exception as exc:
                logger.debug("[RAG] validation step failed (keeping all): %s", exc)

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
        if run_stats is not None:
            async with stats_lock:
                run_stats.entities_enriched += 1
        return entity_copy

    enriched = list(await asyncio.gather(*(_enrich_one(e) for e in entities)))

    if run_stats is not None:
        run_stats.total_rag_ms = (time.perf_counter() - wall_t0) * 1000

    return enriched
