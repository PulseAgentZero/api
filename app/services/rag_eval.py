"""Post-pipeline RAG eval regression: recall@k on synthetic behavioural clusters."""

from __future__ import annotations

import dataclasses
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.config.settings import settings
from app.infrastructure.external_services.qdrant import QdrantService
from app.infrastructure.external_services.rag import embed_and_store_profiles, enrich_entities_with_similar

logger = logging.getLogger(__name__)

# ── Synthetic fixtures (3 clusters × 3 profiles + 3 probes) ─────────────────

_PROFILES: list[dict[str, Any]] = [
    # premium
    {"entity_id": "PRM-001", "profile_summary": "premium subscriber long tenure high monthly spend stable usage", "behavioural_metrics": {"tenure_months": 48, "monthly_spend": 45000, "complaints_30d": 0}, "_cluster": "premium"},
    {"entity_id": "PRM-002", "profile_summary": "premium subscriber multi-year tenure high spend low complaints stable", "behavioural_metrics": {"tenure_months": 36, "monthly_spend": 42000, "complaints_30d": 0}, "_cluster": "premium"},
    {"entity_id": "PRM-003", "profile_summary": "premium high-value subscriber stable usage no complaints long tenure", "behavioural_metrics": {"tenure_months": 60, "monthly_spend": 50000, "complaints_30d": 1}, "_cluster": "premium"},
    # churn
    {"entity_id": "CHN-001", "profile_summary": "declining usage zero recharges 45 days multiple complaints short tenure", "behavioural_metrics": {"tenure_months": 4, "monthly_spend": 2000, "complaints_30d": 3}, "_cluster": "churn"},
    {"entity_id": "CHN-002", "profile_summary": "rapid decline in usage rising complaints open tickets short tenure churn risk", "behavioural_metrics": {"tenure_months": 6, "monthly_spend": 1500, "complaints_30d": 4}, "_cluster": "churn"},
    {"entity_id": "CHN-003", "profile_summary": "inactive subscriber long gap last recharge multiple unresolved complaints", "behavioural_metrics": {"tenure_months": 3, "monthly_spend": 0, "complaints_30d": 2}, "_cluster": "churn"},
    # upgrade
    {"entity_id": "UPG-001", "profile_summary": "high data usage on basic plan growing month-over-month low complaints", "behavioural_metrics": {"tenure_months": 12, "monthly_spend": 3500, "complaints_30d": 0}, "_cluster": "upgrade"},
    {"entity_id": "UPG-002", "profile_summary": "exceeding basic plan allocation growing usage no complaints upgrade candidate", "behavioural_metrics": {"tenure_months": 18, "monthly_spend": 4000, "complaints_30d": 1}, "_cluster": "upgrade"},
    {"entity_id": "UPG-003", "profile_summary": "consistently high data usage cheap plan low complaints willing to pay more", "behavioural_metrics": {"tenure_months": 24, "monthly_spend": 3000, "complaints_30d": 0}, "_cluster": "upgrade"},
]

_PROBES: list[dict[str, Any]] = [
    {
        "entity_id": "PROBE-PREMIUM",
        "_cluster": "premium",
        "profile": {"profile_summary": "premium long-tenure high-spend subscriber", "behavioural_metrics": {"tenure_months": 42, "monthly_spend": 44000}},
        "signal_values": {"complaints_30d": 0},
    },
    {
        "entity_id": "PROBE-CHURN",
        "_cluster": "churn",
        "profile": {"profile_summary": "declining usage rising complaints short tenure churn risk", "behavioural_metrics": {"tenure_months": 5, "monthly_spend": 1800}},
        "signal_values": {"complaints_30d": 3},
    },
    {
        "entity_id": "PROBE-UPGRADE",
        "_cluster": "upgrade",
        "profile": {"profile_summary": "high data usage cheap plan upgrade opportunity", "behavioural_metrics": {"tenure_months": 15, "monthly_spend": 3500}},
        "signal_values": {"complaints_30d": 0},
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
    """Run recall@3 regression on synthetic clusters. Non-fatal; always returns a report."""
    if not settings.is_voyage_configured() or not settings.is_qdrant_configured():
        logger.debug("[RagEval] Skipped — Voyage or Qdrant not configured")
        return RagEvalReport(skipped=True)

    temp_org = f"eval-{org_id[:8]}-{uuid.uuid4().hex[:6]}"
    qdrant = QdrantService()

    try:
        await embed_and_store_profiles(temp_org, _PROFILES)

        enriched = await enrich_entities_with_similar(
            temp_org,
            _PROBES,
            limit=3,
            prefetch_limit=10,
            score_threshold=0.0,
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

        # Scorecard log
        logger.info(
            "[RagEval] Recall regression for org=%s  avg_recall@3=%.2f  passed=%s",
            org_id, avg, passed,
        )
        for s in cluster_scores:
            logger.info(
                "[RagEval]   cluster=%-10s  recall@3=%.2f  mrr=%.2f  hit=%.0f",
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
