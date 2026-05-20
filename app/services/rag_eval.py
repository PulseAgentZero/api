"""Post-pipeline RAG eval regression: recall@k on synthetic behavioural cohorts."""

from __future__ import annotations

import dataclasses
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.config.settings import settings
from app.infrastructure.external_services.qdrant import QdrantService
from app.infrastructure.external_services.rag import RagConfig, embed_and_store_profiles, enrich_entities_with_similar

logger = logging.getLogger(__name__)

# Domain-agnostic synthetic cohorts (3 clusters × 3 profiles + 3 probes).
# Vocabulary is aligned within each cluster so recall@k measures retrieval quality, not any single client's schema or goal label.

_PROFILES: list[dict[str, Any]] = [
    # high_stability — long relationship, strong activity, few incidents
    {
        "entity_id": "EVL-H01",
        "profile_summary": "long relationship length high activity volume stable pattern low incident count",
        "behavioural_metrics": {"relationship_length": 48, "activity_volume": 45000, "incident_count_30d": 0},
        "_cluster": "high_stability",
    },
    {
        "entity_id": "EVL-H02",
        "profile_summary": "established relationship high activity volume consistent engagement few incidents",
        "behavioural_metrics": {"relationship_length": 36, "activity_volume": 42000, "incident_count_30d": 0},
        "_cluster": "high_stability",
    },
    {
        "entity_id": "EVL-H03",
        "profile_summary": "mature relationship strong activity volume stable engagement minimal incidents",
        "behavioural_metrics": {"relationship_length": 60, "activity_volume": 50000, "incident_count_30d": 1},
        "_cluster": "high_stability",
    },
    # low_engagement — declining activity, gaps, elevated incidents, short relationship
    {
        "entity_id": "EVL-L01",
        "profile_summary": "declining event frequency long inactivity window elevated incident count short relationship",
        "behavioural_metrics": {"relationship_length": 4, "activity_volume": 2000, "incident_count_30d": 3, "event_frequency_90d": 1},
        "_cluster": "low_engagement",
    },
    {
        "entity_id": "EVL-L02",
        "profile_summary": "rapid activity decline rising incident count open issues short relationship attrition pattern",
        "behavioural_metrics": {"relationship_length": 6, "activity_volume": 1500, "incident_count_30d": 4, "event_frequency_90d": 2},
        "_cluster": "low_engagement",
    },
    {
        "entity_id": "EVL-L03",
        "profile_summary": "dormant profile long gap since last event multiple unresolved incidents likely disengagement",
        "behavioural_metrics": {"relationship_length": 3, "activity_volume": 0, "incident_count_30d": 2, "event_frequency_90d": 0},
        "_cluster": "low_engagement",
    },
    # rising_utilization — growing usage relative to footprint, upgrade-style signal
    {
        "entity_id": "EVL-R01",
        "profile_summary": "high utilization on baseline tier growing month over month low incident count",
        "behavioural_metrics": {"relationship_length": 12, "activity_volume": 3500, "incident_count_30d": 0},
        "_cluster": "rising_utilization",
    },
    {
        "entity_id": "EVL-R02",
        "profile_summary": "exceeding baseline allocation increasing activity volume few incidents expansion candidate",
        "behavioural_metrics": {"relationship_length": 18, "activity_volume": 4000, "incident_count_30d": 1},
        "_cluster": "rising_utilization",
    },
    {
        "entity_id": "EVL-R03",
        "profile_summary": "consistently high utilization modest tier low incidents capacity for higher tier",
        "behavioural_metrics": {"relationship_length": 24, "activity_volume": 3000, "incident_count_30d": 0},
        "_cluster": "rising_utilization",
    },
]

_PROBES: list[dict[str, Any]] = [
    {
        "entity_id": "PROBE-HIGH-STABILITY",
        "_cluster": "high_stability",
        "profile": {
            "profile_summary": "long relationship high activity volume stable engagement",
            "behavioural_metrics": {"relationship_length": 42, "activity_volume": 44000},
        },
        "signal_values": {"incident_count_30d": 0},
    },
    {
        "entity_id": "PROBE-LOW-ENGAGEMENT",
        "_cluster": "low_engagement",
        "profile": {
            "profile_summary": "declining event frequency rising incident count short relationship disengagement",
            "behavioural_metrics": {
                "relationship_length": 5,
                "activity_volume": 1800,
                "event_frequency_90d": 2,
            },
        },
        "signal_values": {"incident_count_30d": 3, "event_frequency_90d": 2},
    },
    {
        "entity_id": "PROBE-RISING-UTILIZATION",
        "_cluster": "rising_utilization",
        "profile": {
            "profile_summary": "high utilization baseline tier expansion opportunity",
            "behavioural_metrics": {"relationship_length": 15, "activity_volume": 3500},
        },
        "signal_values": {"incident_count_30d": 0},
    },
]


def _cluster_ids(cluster: str) -> set[str]:
    return {p["entity_id"] for p in _PROFILES if p["_cluster"] == cluster}


# ── Inline metrics (avoids cross-importing from tests/) ──────────────────────

def _recall_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    return len(set(retrieved[:k]) & relevant) / len(relevant)


def _mrr(retrieved: list[str], relevant: set[str]) -> float:
    for idx, rid in enumerate(retrieved, start=1):
        if rid in relevant:
            return 1.0 / idx
    return 0.0


def _hit_rate(retrieved: list[str], relevant: set[str]) -> float:
    return 1.0 if any(r in relevant for r in retrieved) else 0.0


# ── Report dataclass ─────────────────────────────────────────────────────────

@dataclass
class RagEvalReport:
    """Results of one synthetic recall regression."""

    skipped: bool = False
    cluster_scores: list[dict[str, Any]] = field(default_factory=list)
    avg_recall_at_3: float = 0.0
    passed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# ── Main service function ─────────────────────────────────────────────────────

async def run_rag_eval_regression(org_id: str) -> RagEvalReport:
    """Run recall@3 regression on synthetic cohorts. Non-fatal; always returns a report."""
    if not settings.is_voyage_configured() or not settings.is_qdrant_configured():
        logger.debug("[RagEval] Skipped — Voyage or Qdrant not configured")
        return RagEvalReport(skipped=True)

    temp_org = f"eval-{org_id[:8]}-{uuid.uuid4().hex[:6]}"
    qdrant = QdrantService()

    try:
        await embed_and_store_profiles(temp_org, _PROFILES)

        _eval_config = RagConfig(
            top_k=3,
            prefetch_limit=max(len(_PROFILES) * 4, 36),
            score_threshold=0.0,
            freshness_days=0,
            enable_rerank=True,
            enable_hybrid=True,
            enable_query_rewrite=False,
            enable_autocut=False,
            rerank_model=settings.VOYAGE_RERANK_MODEL,
            enable_hierarchical_chunks=False,
            enable_retrieval_validation=False,
            enable_query_expansion=False,
        )
        enriched = await enrich_entities_with_similar(
            temp_org,
            _PROBES,
            config=_eval_config,
        )

        cluster_scores: list[dict[str, Any]] = []
        for probe in enriched:
            cluster = probe["_cluster"]
            retrieved = [s["entity_id"] for s in probe.get("similar_entities", [])]
            relevant = _cluster_ids(cluster)
            cluster_scores.append({
                "cluster": cluster,
                "recall_at_3": _recall_at_k(retrieved, relevant, k=3),
                "mrr": _mrr(retrieved, relevant),
                "hit_rate": _hit_rate(retrieved, relevant),
            })

        avg = sum(s["recall_at_3"] for s in cluster_scores) / len(cluster_scores) if cluster_scores else 0.0
        passed = avg >= settings.RAG_EVAL_RECALL_THRESHOLD

        logger.info(
            "[RagEval] Recall regression for org=%s  avg_recall@3=%.2f  passed=%s",
            org_id, avg, passed,
        )
        for s in cluster_scores:
            logger.info(
                "[RagEval]   cluster=%-20s  recall@3=%.2f  mrr=%.2f  hit=%.0f",
                s["cluster"], s["recall_at_3"], s["mrr"], s["hit_rate"],
            )
        if not passed:
            logger.warning(
                "[RagEval] avg recall@3 %.2f is below threshold %.2f for org=%s",
                avg, settings.RAG_EVAL_RECALL_THRESHOLD, org_id,
            )

        return RagEvalReport(
            skipped=False,
            cluster_scores=cluster_scores,
            avg_recall_at_3=round(avg, 4),
            passed=passed,
        )

    except Exception as exc:
        logger.warning("[RagEval] eval regression failed for org=%s: %s", org_id, exc)
        return RagEvalReport(skipped=True)

    finally:
        try:
            await qdrant.delete_org_collection(temp_org)
            await qdrant.close()
        except Exception:
            pass
