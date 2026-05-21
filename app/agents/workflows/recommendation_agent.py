"""Recommendation Agent — generates personalised recommendations.

Runs last in the pipeline. Takes scored entities with risk >= 0.6 and
generates actionable, entity-specific recommendations using LLM.
Writes results to Pulse's own recommendations table.

Provider: Groq (openai/gpt-oss-120b)
Rationale: Recommendation quality is directly user-facing. The 120B model produces substantially better instruction following on multi-constraint tasks like reasoning about
specific signal combinations to generate tailored interventions.
"""

import json
import logging
import re
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import BaseAgent, LLMProvider, repair_truncated_json
from app.agents.prompts.recommendation import RECOMMENDATION_PROMPT
from app.agents.state import PipelineState
from app.infrastructure.database.base import touch_updated_at
from app.infrastructure.database.repositories.recommendation_repository import (
    RecommendationRepository,
)
from app.infrastructure.database.client_queries import get_schema_mapping
from app.infrastructure.external_services.rag import (
    RagConfig,
    RagRunStats,
    _merge_rag_stats,
    enrich_entities_with_similar,
)
from app.config.settings import settings
from app.services.procedural_memory import format_procedural_block

logger = logging.getLogger(__name__)

DEFAULT_RECOMMENDATION_LIMIT = 50

_ML_JARGON_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d+(?:\.\d+)?%\s*importance\b", re.I), ""),
    (re.compile(r"\bfeature\s+importances?\b", re.I), "main factors"),
    (re.compile(r"\bml\s+model\b", re.I), "Entivia"),
    (re.compile(r"\b(\d*\.?\d+)\s+predicted chance of\b", re.I), r"\1 likelihood of"),
    (re.compile(r"\bfeature\s*\(([^)]+)\)\s*\([^)]*\)", re.I), r"\1"),
    (re.compile(r"\badds?\s+\d+(?:\.\d+)?%\s+importance[,.]?\s*", re.I), "also matters. "),
    (re.compile(r"\s{2,}"), " "),
]


def _humanize_signal_key(key: str) -> str:
    cleaned = re.sub(r"[_-]+", " ", str(key)).strip()
    if not cleaned:
        return str(key)
    return cleaned.title()


def _slim_entity_for_recommendation(entity: dict) -> dict:
    """Payload for the LLM: business facts only, no ML diagnostics."""
    signals = entity.get("signal_values") or {}
    key_facts: dict[str, Any] = {}
    for k, v in signals.items():
        if isinstance(v, (int, float, str, bool)) or v is None:
            key_facts[_humanize_signal_key(str(k))] = v

    slim: dict[str, Any] = {
        "entity_id": entity.get("entity_id"),
        "entity_name": entity.get("entity_name"),
        "risk_level": entity.get("risk_tier"),
        "priority_score": round(float(entity.get("risk_score", 0) or 0), 3),
        "key_facts": key_facts,
    }
    narrative = str(entity.get("risk_narrative") or "").strip()
    if narrative:
        slim["analyst_note"] = narrative[:400]
    similar = entity.get("similar_entities") or []
    if similar:
        slim["similar_cases"] = []
        for s in similar[:3]:
            case: dict[str, Any] = {
                "entity_id": s.get("entity_id"),
                "summary": (s.get("profile_summary") or "")[:180],
                "risk_level": s.get("risk_tier"),
            }
            past = s.get("past_recommendations")
            if past:
                case["past_recommendations"] = [
                    {
                        "type": pr.get("type"),
                        "urgency": pr.get("urgency"),
                        "title": pr.get("title"),
                        "suggested_action": (pr.get("suggested_action") or "")[:200],
                        "status": pr.get("status"),
                    }
                    for pr in past[:5]
                    if isinstance(pr, dict)
                ]
            slim["similar_cases"].append(case)
    return slim


def _businessize_copy(text: str) -> str:
    """Strip common ML phrases that slip through the prompt."""
    if not text:
        return text
    out = text.strip()
    for pattern, repl in _ML_JARGON_PATTERNS:
        out = pattern.sub(repl, out)
    return re.sub(r"\s+([,.])", r"\1", out).strip()


class RecommendationAgent(BaseAgent):
    """Generates personalised, actionable recommendations for at-risk entities.

    Uses Groq GPT-OSS-120B for maximum reasoning quality on user-facing output.
    Falls back to template-based recommendations if LLM fails.
    """

    def __init__(self) -> None:
        super().__init__(
            name="RecommendationAgent",
            provider=LLMProvider.GROQ,
            default_model=settings.GROQ_LLM_MODEL_HEAVY,
        )

    async def run(
        self, state: PipelineState, db: AsyncSession
    ) -> PipelineState:
        """Generate recommendations for elevated-risk entities."""

        org_id = UUID(state["org_id"])
        mapping_id = UUID(str(state["mapping_id"])) if state.get("mapping_id") else None
        scored = state.get("scored_entities", [])
        recommendation_limit = DEFAULT_RECOMMENDATION_LIMIT

        elevated = [e for e in scored if e.get("risk_score", 0) >= 0.6]
        at_risk = elevated[:recommendation_limit]

        # Record any cap so the pipeline run row carries the sampling note.
        if len(elevated) > recommendation_limit:
            caps = dict(state.get("generation_caps") or {})
            caps["recommendations"] = {
                "elevated_total": len(elevated),
                "limit": recommendation_limit,
                "truncated": True,
            }
            state["generation_caps"] = caps

        if not at_risk:
            logger.info("[RecommendationAgent] No at-risk entities to recommend for")
            state["recommendations"] = []
            state["recommendation_stats"] = {"total_generated": 0, "total_persisted": 0}
            return state

        # Generate recommendations via LLM
        prompt = RECOMMENDATION_PROMPT.format(
            org_name=state.get("org_name", "Unknown"),
            industry=state.get("industry", "Unknown"),
            business_context=state.get("business_context", ""),
            entity_label=state.get("entity_label", "entities"),
            goal_label=state.get("goal_label", "improve operations"),
            recommendation_limit=recommendation_limit,
            procedural_block=format_procedural_block(
                state.get("procedural_learnings")
            ),
        )

        # Attach profile context (in-memory only) so the LLM can reason over
        # behavioural signals when crafting interventions. Profiles are never
        # persisted to the Pulse application database.
        profile_index = {
            str(p.get("entity_id")): p
            for p in (state.get("entity_profiles") or [])
            if p.get("entity_id") is not None
        }
        enriched_at_risk = [
            _slim_entity_for_recommendation(
                _augment_with_profile(e, profile_index.get(e.get("entity_id")))
            )
            for e in at_risk
        ]

        # Build a compact past-recommendation index once, keyed by entity_id, so
        # RAG enrichment can attach prior interventions for similar entities
        # without re-querying Pulse DB per entity.
        past_recs_by_entity = await _load_past_recs_by_entity(db, org_id)

        # Resolve per-org RAG config once and reuse across batches.
        try:
            _mapping = await get_schema_mapping(db, org_id, mapping_id=mapping_id)
            _rag_config = RagConfig.resolve(getattr(_mapping, "rag_config", None))
        except Exception:
            _rag_config = RagConfig.from_defaults()

        all_recs: list[dict] = []
        batch_size = 20

        for i in range(0, len(enriched_at_risk), batch_size):
            batch = enriched_at_risk[i : i + batch_size]
            try:
                _rag_stats = RagRunStats()
                batch = await enrich_entities_with_similar(
                    str(org_id),
                    batch,
                    config=_rag_config,
                    past_recs_by_entity=past_recs_by_entity,
                    run_stats=_rag_stats,
                )
                state["rag_run_stats"] = _merge_rag_stats(
                    state.get("rag_run_stats") or {}, _rag_stats.to_dict()
                )
                raw_batch_recs = await self._generate_batch(prompt, state, batch)
                batch_recs = self._normalize_recommendations(raw_batch_recs, batch, state)
                all_recs.extend(batch_recs)
            except Exception as e:
                logger.error(
                    "[RecommendationAgent] Batch %d failed: %s", i // batch_size, e
                )
                all_recs.extend(self._fallback_recommendations(batch, state))

        # Persist atomically — supersede existing active recs and create new
        # ones inside a single SAVEPOINT so a mid-loop failure cannot leave
        # the org with no active recommendations.
        created = 0
        superseded = 0
        try:
            async with db.begin_nested():
                repo = RecommendationRepository(db)
                existing = await repo.list_by_org(org_id, status="open")
                for rec in existing:
                    rec.status = "superseded"
                    touch_updated_at(rec)
                superseded = len(existing)

                pipeline_run_id: UUID | None = None
                if state.get("pipeline_run_id"):
                    try:
                        pipeline_run_id = UUID(str(state["pipeline_run_id"]))
                    except (ValueError, TypeError):
                        pipeline_run_id = None

                for rec_data in all_recs:
                    await repo.create(
                        org_id=org_id,
                        entity_id=str(rec_data.get("entity_id", "")),
                        entity_label=rec_data.get("entity_name"),
                        type=rec_data.get("type", "retention_intervention"),
                        urgency=rec_data.get("urgency", "high"),
                        title=rec_data.get("title", "Risk intervention required"),
                        reasoning=rec_data.get("reasoning", ""),
                        suggested_action=rec_data.get("suggested_action", ""),
                        expected_impact=rec_data.get("expected_impact"),
                        status="open",
                        pipeline_run_id=pipeline_run_id,
                    )
                    created += 1

            logger.info(
                "[RecommendationAgent] Persisted: %d new recs, %d superseded",
                created, superseded,
            )
        except Exception as e:
            # SAVEPOINT auto-rolled back; original active recs remain intact.
            logger.error("[RecommendationAgent] DB persistence failed (rolled back): %s", e)
            state["error"] = f"Recommendation persistence failed: {e}"
            created = 0

        # Build stats
        by_urgency: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for rec in all_recs:
            urg = rec.get("urgency", "high")
            by_urgency[urg] = by_urgency.get(urg, 0) + 1
            rtype = rec.get("type", "other")
            by_type[rtype] = by_type.get(rtype, 0) + 1

        if created > 0:
            critical_n = by_urgency.get("critical", 0)
            high_n = by_urgency.get("high", 0)
            if critical_n or high_n:
                pipeline_run_id: UUID | None = None
                if state.get("pipeline_run_id"):
                    try:
                        pipeline_run_id = UUID(str(state["pipeline_run_id"]))
                    except (ValueError, TypeError):
                        pipeline_run_id = None
                try:
                    from app.services.notification_service import (
                        notify_high_priority_recommendations,
                    )

                    await notify_high_priority_recommendations(
                        db,
                        org_id,
                        critical_count=critical_n,
                        high_count=high_n,
                        pipeline_run_id=pipeline_run_id,
                    )
                except Exception as notify_exc:
                    logger.warning(
                        "[RecommendationAgent] In-app notification failed: %s",
                        notify_exc,
                    )

        state["recommendations"] = all_recs
        state["recommendation_stats"] = {
            "total_generated": created,
            "total_drafted": len(all_recs),
            "total_persisted": created,
            "total_superseded": superseded,
            "by_urgency": by_urgency,
            "by_type": by_type,
        }
        state["reasoning_log"].extend(self._reasoning_entries)

        logger.info(
            "[RecommendationAgent] Complete: %d drafted, %d persisted",
            len(all_recs), created,
        )
        return state

    async def _generate_batch(
        self,
        system_prompt: str,
        state: PipelineState,
        batch: list[dict],
    ) -> list[dict]:
        """Generate recommendations for a batch of entities via GPT-OSS-120B."""

        entity_data = json.dumps(batch, default=str)
        user_prompt = (
            f"Generate business-friendly recommendations for these {len(batch)} "
            f"at-risk {state.get('entity_label', 'entities')}. "
            f"Write for a manager, not a data scientist: use key_facts in plain English, "
            f"never mention features, importance %, or model scores.\n\n"
            f"Entities:\n{entity_data}"
        )

        raw = await self.llm_json_call(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=16384,
        )

        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            repaired = repair_truncated_json(raw)
            if repaired is not None:
                logger.warning(
                    "[RecommendationAgent] JSON truncated by LLM, repaired successfully"
                )
                result = json.loads(repaired)
            else:
                raise
        if isinstance(result, dict) and "recommendations" in result:
            return result["recommendations"]
        if isinstance(result, list):
            return result
        return []

    @staticmethod
    def _normalize_recommendations(
        recommendations: list[Any], batch: list[dict], state: PipelineState
    ) -> list[dict]:
        """Keep valid LLM recommendations and fill gaps with deterministic fallbacks."""
        batch_by_id = {
            str(entity.get("entity_id")): entity
            for entity in batch
            if entity.get("entity_id") is not None
        }
        normalized: list[dict] = []
        seen: set[str] = set()
        for rec in recommendations:
            if not isinstance(rec, dict):
                continue
            entity_id = str(rec.get("entity_id") or "").strip()
            entity = batch_by_id.get(entity_id)
            if not entity or entity_id in seen:
                continue
            seen.add(entity_id)
            title = _businessize_copy(rec.get("title") or "Review required")
            reasoning = _businessize_copy(rec.get("reasoning") or "")
            action = _businessize_copy(rec.get("suggested_action") or "")
            impact = _businessize_copy(rec.get("expected_impact") or "")
            normalized.append({
                "entity_id": entity_id,
                "entity_name": rec.get("entity_name") or entity.get("entity_name"),
                "risk_score": rec.get("risk_score", entity.get("priority_score", 0)),
                "risk_tier": rec.get("risk_tier", entity.get("risk_level", "high")),
                "type": rec.get("type") or "retention_intervention",
                "urgency": rec.get("urgency") or "high",
                "title": title or "Review required",
                "reasoning": reasoning,
                "suggested_action": action,
                "expected_impact": impact or None,
            })

        missing = [
            entity
            for entity in batch
            if str(entity.get("entity_id")) not in seen
        ]
        if missing:
            normalized.extend(RecommendationAgent._fallback_recommendations(missing, state))
        return normalized

    @staticmethod
    def _fallback_recommendations(
        batch: list[dict], state: PipelineState
    ) -> list[dict]:
        """Generate template-based recommendations as fallback when LLM fails."""
        recs = []
        label = state.get("entity_label", "entity")
        for entity in batch:
            tier = entity.get("risk_level") or entity.get("risk_tier") or "high"
            eid = entity.get("entity_id", "")
            facts = entity.get("key_facts") or entity.get("signal_values") or {}
            top_label = "several factors"
            if facts:
                top_key = max(facts, key=lambda k: _to_float(facts[k]))
                top_label = _humanize_signal_key(str(top_key))

            recs.append({
                "entity_id": eid,
                "entity_name": entity.get("entity_name"),
                "risk_score": entity.get("priority_score", entity.get("risk_score", 0)),
                "risk_tier": tier,
                "type": "account_review",
                "urgency": "critical" if tier == "critical" else "high",
                "title": f"Review {eid} — {tier} priority",
                "reasoning": (
                    f"This {label.rstrip('s')} ({eid}) needs attention: {top_label} "
                    f"stands out compared to similar cases. "
                    f"Review details before the next approval step."
                ),
                "suggested_action": (
                    f"Open the profile for {eid}, confirm the latest activity, "
                    f"and assign a follow-up aligned with {state.get('goal_label', 'your goal')}."
                ),
                "expected_impact": (
                    "Early review usually prevents avoidable losses on flagged cases."
                ),
            })
        return recs


def _to_float(v: object) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _augment_with_profile(entity: dict, profile: dict | None) -> dict:
    """Merge profiling fields into an entity payload (in-memory only, not persisted)."""
    if not profile:
        return entity
    enriched = dict(entity)
    profile_fields = {
        k: v
        for k, v in profile.items()
        if k not in {"entity_id", "entity_name", "risk_score", "risk_tier", "signals"}
    }
    if profile_fields:
        enriched["profile"] = profile_fields
    return enriched


async def _load_past_recs_by_entity(
    db: AsyncSession, org_id: UUID
) -> dict[str, list[dict]]:
    """Index all org recommendations by entity_id with compact fields."""
    try:
        recs = await RecommendationRepository(db).list_by_org(org_id)
    except Exception as exc:
        logger.warning(
            "[RecommendationAgent] Could not load past recommendations: %s", exc
        )
        return {}

    grouped: dict[str, list[dict]] = {}
    for rec in recs:
        if not rec.entity_id:
            continue
        grouped.setdefault(str(rec.entity_id), []).append({
            "type": rec.type,
            "urgency": rec.urgency,
            "title": rec.title,
            "suggested_action": rec.suggested_action,
            "status": rec.status,
        })
    return grouped
