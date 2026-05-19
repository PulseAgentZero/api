"""Risk Scoring Agent — computes risk scores and generates narratives.

Runs third in the pipeline. Uses the entity profiles from Agent 2
and the org's risk configuration to score every entity deterministically,
then uses LLM to generate risk narratives for elevated entities.

Provider: Groq (openai/gpt-oss-120b) for narratives
Rationale: Risk narratives need reasoning depth about signal combinations.
The 120B model handles multi-constraint reasoning significantly better
than the 70B. Scores themselves are deterministic (computed by compute_risk).
"""

import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import BaseAgent, LLMProvider, repair_truncated_json
from app.agents.prompts.risk_scoring import RISK_SCORING_PROMPT
from app.agents.state import PipelineState
from app.infrastructure.database.client_queries import compute_risk, fetch_entities, get_schema_mapping
from app.infrastructure.external_services.rag import (
    RagConfig,
    RagRunStats,
    _merge_rag_stats,
    embed_and_store_profiles,
    enrich_entities_with_similar,
    update_entity_metadata,
)
from app.config.settings import settings
from app.services.procedural_memory import format_procedural_block

logger = logging.getLogger(__name__)

DEFAULT_RAG_INDEX_LIMIT = 1000
NARRATIVE_BATCH_SIZE = 8


class RiskScoringAgent(BaseAgent):
    """Scores entities using deterministic risk model + LLM narratives.

    Uses deterministic compute_risk() for scores.
    Uses Groq GPT-OSS-120B for risk narrative generation.
    """

    def __init__(self) -> None:
        super().__init__(
            name="RiskScoringAgent",
            provider=LLMProvider.GROQ,
            default_model=settings.GROQ_LLM_MODEL_HEAVY,
        )

    async def run(
        self, state: PipelineState, db: AsyncSession
    ) -> PipelineState:
        """Execute risk scoring with ML predictions or deterministic fallback."""

        org_id = UUID(state["org_id"])
        mapping_id = UUID(str(state["mapping_id"])) if state.get("mapping_id") else None
        use_ml = state.get("ml_available") and state.get("ml_scored_entities")

        if use_ml:
            # ── ML-first path: use predictions from Model Training Agent ──
            logger.info("[RiskScoringAgent] Using ML-predicted risk scores")
            ml_scored = state["ml_scored_entities"]

            # ── Validate ML scores before using them ──
            invalid_scores = [
                e for e in ml_scored
                if not isinstance(e.get("risk_score"), (int, float))
                or e["risk_score"] < 0.0 or e["risk_score"] > 1.0
            ]
            if invalid_scores:
                logger.warning(
                    "[RiskScoringAgent] %d ML scores outside [0,1] range — "
                    "falling back to rule-based scoring",
                    len(invalid_scores),
                )
                use_ml = False

        if use_ml:
            ml_scored = state["ml_scored_entities"]

            # Fetch entity names and signal values for display/narratives
            try:
                mapping = await get_schema_mapping(db, org_id, mapping_id=mapping_id)
                entities = await fetch_entities(db, org_id, mapping)
                id_col = mapping.entity_id_col
                name_col = mapping.entity_name_col
                name_lookup = {
                    str(e[id_col]): str(e.get(name_col)) if name_col and e.get(name_col) else None
                    for e in entities
                }
                signal_lookup = {}
                for e in entities:
                    eid = str(e[id_col])
                    signal_lookup[eid] = {
                        sig_label: e.get(col_name)
                        for sig_label, col_name in (mapping.signal_columns or {}).items()
                        if col_name in e
                    }
            except Exception as e:
                logger.warning("[RiskScoringAgent] Failed to fetch entity names: %s", e)
                name_lookup = {}
                signal_lookup = {}

            # ── Validate entity coverage ──
            if entities:
                coverage = len(ml_scored) / len(entities)
                if coverage < 0.5:
                    logger.warning(
                        "[RiskScoringAgent] ML scored only %d of %d entities (%.1f%%) — "
                        "falling back to rule-based scoring for better coverage",
                        len(ml_scored), len(entities), coverage * 100,
                    )
                    use_ml = False

        if use_ml:
            scored_entities = []
            for ml_entity in ml_scored:
                eid = str(ml_entity["entity_id"])
                score = float(ml_entity["risk_score"])
                # Clamp score and RE-DERIVE tier (single source of truth)
                score = max(0.0, min(1.0, score))

                if score >= 0.8:
                    tier = "critical"
                elif score >= 0.6:
                    tier = "high"
                elif score >= 0.4:
                    tier = "medium"
                else:
                    tier = "low"

                scored_entities.append({
                    "entity_id": eid,
                    "entity_name": name_lookup.get(eid),
                    "risk_score": round(score, 4),
                    "risk_tier": tier,
                    "signal_values": signal_lookup.get(eid, {}),
                    "risk_narrative": None,
                    "scoring_method": "ml",
                })
        else:
            # ── Deterministic fallback: use compute_risk() ──
            if state.get("ml_available"):
                logger.info("[RiskScoringAgent] ML validation failed — falling back to rule-based scoring")
            else:
                logger.info("[RiskScoringAgent] Using deterministic rule-based scoring")

            try:
                mapping = await get_schema_mapping(db, org_id, mapping_id=mapping_id)
                entities = await fetch_entities(db, org_id, mapping)
                scored = compute_risk(entities, mapping.signal_columns, mapping.risk_config)
            except Exception as e:
                logger.error("[RiskScoringAgent] Failed to compute risk: %s", e)
                state["scored_entities"] = []
                state["risk_summary"] = {"error": str(e)}
                state["error"] = f"Risk scoring failed: {e}"
                state["reasoning_log"].extend(self._reasoning_entries)
                return state

            id_col = mapping.entity_id_col
            name_col = mapping.entity_name_col
            scored_entities = []
            for entity in scored:
                scored_entities.append({
                    "entity_id": str(entity[id_col]),
                    "entity_name": str(entity.get(name_col)) if name_col and entity.get(name_col) else None,
                    "risk_score": entity["risk_score"],
                    "risk_tier": entity["risk_tier"],
                    "signal_values": entity.get("signals", {}),
                    "risk_narrative": None,
                    "scoring_method": "rule_based",
                })

        # Sort by risk score descending
        scored_entities.sort(key=lambda e: e["risk_score"], reverse=True)

        # Generate LLM narratives for elevated entities (risk_score >= 0.6)
        elevated = [e for e in scored_entities if e["risk_score"] >= 0.6]

        narrative_cap = 50
        narratives_target = elevated[:narrative_cap]

        if len(elevated) > narrative_cap:
            caps = dict(state.get("generation_caps") or {})
            caps["narratives"] = {
                "elevated_total": len(elevated),
                "limit": narrative_cap,
                "truncated": True,
            }
            state["generation_caps"] = caps

        # Pull per-entity profiles from Agent 2 so narratives can reason over
        # behavioural context, not just deterministic signal values.
        profile_index = {
            str(p.get("entity_id")): p
            for p in (state.get("entity_profiles") or [])
            if p.get("entity_id") is not None
        }

        if narratives_target:
            payload = [
                _augment_with_profile(e, profile_index.get(e["entity_id"]))
                for e in narratives_target
            ]

            # When ML is active, enrich narrative context with feature importances
            if use_ml and state.get("feature_importances"):
                for p in payload:
                    p["ml_feature_importances"] = state["feature_importances"][:10]
                    p["scoring_method"] = "ml"

            # RAG: attach similar past-profile entities so narratives can reference
            # precedent. Per-org overrides come from schema_mapping.rag_config.
            try:
                _mapping = await get_schema_mapping(db, org_id, mapping_id=mapping_id)
                _rag_overrides = getattr(_mapping, "rag_config", None)
            except Exception:
                _rag_overrides = None
            _rag_stats = RagRunStats()
            payload = await enrich_entities_with_similar(
                str(org_id),
                payload,
                config=RagConfig.resolve(_rag_overrides),
                run_stats=_rag_stats,
            )
            state["rag_run_stats"] = _merge_rag_stats(
                state.get("rag_run_stats") or {}, _rag_stats.to_dict()
            )

            try:
                narratives = await self._generate_narratives(state, payload)
                narrative_map = {
                    str(n["entity_id"]): n.get("risk_narrative", "")
                    for n in narratives
                    if n.get("entity_id") is not None
                }
                for entity in scored_entities:
                    eid = str(entity["entity_id"])
                    if eid in narrative_map and narrative_map[eid]:
                        entity["risk_narrative"] = narrative_map[eid]
            except Exception as e:
                logger.warning("[RiskScoringAgent] Narrative generation failed (non-fatal): %s", e)
                # Non-fatal — scores are still valid without narratives

        # Build risk summary
        tier_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for entity in scored_entities:
            tier_counts[entity["risk_tier"]] += 1

        # Find most common risk drivers across elevated entities
        if use_ml and state.get("feature_importances"):
            top_signals = [fi["feature"] for fi in state["feature_importances"][:5]]
        else:
            signal_freq: dict[str, int] = {}
            for entity in elevated:
                for signal, value in entity.get("signal_values", {}).items():
                    if isinstance(value, (int, float)) and value > 0:
                        signal_freq[signal] = signal_freq.get(signal, 0) + 1
            top_signals = sorted(signal_freq, key=lambda k: signal_freq[k], reverse=True)[:5]

        scoring_method = "ml" if use_ml else "rule_based"
        model_accuracy = state.get("model_metrics", {}).get("accuracy")

        state["scored_entities"] = scored_entities
        state["risk_summary"] = {
            "total_scored": len(scored_entities),
            "critical_count": tier_counts["critical"],
            "high_count": tier_counts["high"],
            "medium_count": tier_counts["medium"],
            "low_count": tier_counts["low"],
            "top_risk_signals": top_signals,
            "scoring_method": scoring_method,
            "model_accuracy": model_accuracy,
            "key_findings": (
                f"{tier_counts['critical']} critical and {tier_counts['high']} high-risk "
                f"{state.get('entity_label', 'entities')} identified"
                f"{f' using ML model (accuracy: {model_accuracy:.1%})' if model_accuracy else ' using rule-based scoring'}. "
                f"Top risk drivers: {', '.join(top_signals[:3]) if top_signals else 'N/A'}."
            ),
        }
        state["reasoning_log"].extend(self._reasoning_entries)

        rag_index_entities = scored_entities[:DEFAULT_RAG_INDEX_LIMIT]
        try:
            await embed_and_store_profiles(
                str(org_id),
                _profiles_from_scored_entities(rag_index_entities),
            )
        except Exception as e:
            logger.warning("[RiskScoringAgent] RAG profile indexing failed: %s", e)

        # Patch Qdrant payloads with risk_tier + last_scored_at so subsequent
        # cycles can filter retrieval by tier and freshness. Non-fatal on error.
        import time as _time
        _scored_ts = _time.time()
        await update_entity_metadata(
            str(org_id),
            [
                (
                    e["entity_id"],
                    {
                        "risk_tier": e["risk_tier"],
                        "risk_score": e["risk_score"],
                        "last_scored_at": _scored_ts,
                    },
                )
                for e in rag_index_entities
            ],
        )

        logger.info(
            "[RiskScoringAgent] Complete (%s): %d scored, %d critical, %d high",
            scoring_method, len(scored_entities), tier_counts["critical"], tier_counts["high"],
        )
        return state

    async def _generate_narratives(
        self, state: PipelineState, elevated: list[dict]
    ) -> list[dict]:
        """Generate risk narratives for elevated entities in batches."""
        system_prompt = RISK_SCORING_PROMPT.format(
            org_name=state.get("org_name", "Unknown"),
            industry=state.get("industry", "Unknown"),
            goal_label=state.get("goal_label", "improve operations"),
            entity_label=state.get("entity_label", "entities"),
            signal_columns=json.dumps(state.get("signal_columns", {})),
            risk_config=json.dumps(state.get("risk_config", {})),
            procedural_block=format_procedural_block(
                state.get("procedural_learnings")
            ),
        )

        all_narratives: list[dict] = []
        slim_entities = [_slim_narrative_payload(e) for e in elevated]

        for i in range(0, len(slim_entities), NARRATIVE_BATCH_SIZE):
            batch = slim_entities[i : i + NARRATIVE_BATCH_SIZE]
            batch_result = await self._generate_narrative_batch(
                state, system_prompt, batch
            )
            all_narratives.extend(batch_result)

        covered = {str(n.get("entity_id")) for n in all_narratives if n.get("entity_id")}
        missing = [e for e in elevated if str(e.get("entity_id")) not in covered]
        if missing:
            all_narratives.extend(_fallback_narratives(missing, state))

        return all_narratives

    async def _generate_narrative_batch(
        self,
        state: PipelineState,
        system_prompt: str,
        batch: list[dict],
    ) -> list[dict]:
        """LLM narrative generation for one batch with JSON repair."""
        entity_data = json.dumps(batch, default=str)
        user_prompt = (
            f"Generate risk narratives for these {len(batch)} elevated-risk "
            f"{state.get('entity_label', 'entities')}. "
            f"Each entity already has a deterministic risk_score and risk_tier. "
            f"Your job is to write a 1-2 sentence risk_narrative for each one "
            f"explaining WHY their specific signal values make them a priority.\n\n"
            f"Entities:\n{entity_data}"
        )

        raw = await self.llm_json_call(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=16384,
        )

        parsed = _parse_narrative_llm_response(raw)
        if parsed:
            return parsed

        logger.warning(
            "[RiskScoringAgent] Narrative batch parse failed; using fallback for %d entities",
            len(batch),
        )
        return _fallback_narratives(
            [{"entity_id": e.get("entity_id"), **e} for e in batch],
            state,
        )


def _parse_narrative_llm_response(raw: str) -> list[dict]:
    """Parse LLM JSON for scored_entities; repair truncated output when possible."""
    for attempt_raw in (raw, repair_truncated_json(raw)):
        if not attempt_raw:
            continue
        try:
            result = json.loads(attempt_raw)
        except json.JSONDecodeError:
            if attempt_raw is raw:
                logger.warning(
                    "[RiskScoringAgent] Failed to parse narratives JSON (snippet): %s",
                    raw[:500],
                )
            continue
        if isinstance(result, dict) and "scored_entities" in result:
            return result["scored_entities"]
        if isinstance(result, list):
            return result
        logger.warning(
            "[RiskScoringAgent] Narratives JSON missing scored_entities key"
        )
        return []
    return []


def _slim_narrative_payload(entity: dict) -> dict:
    """Shrink entity payload for narrative LLM context."""
    signals = entity.get("signal_values") or {}
    top_signals = sorted(
        ((k, v) for k, v in signals.items() if isinstance(v, (int, float))),
        key=lambda item: abs(item[1]),
        reverse=True,
    )[:6]
    slim: dict[str, Any] = {
        "entity_id": entity.get("entity_id"),
        "entity_name": entity.get("entity_name"),
        "risk_score": entity.get("risk_score"),
        "risk_tier": entity.get("risk_tier"),
        "signal_values": dict(top_signals),
        "scoring_method": entity.get("scoring_method"),
    }
    profile = entity.get("profile")
    if isinstance(profile, dict):
        summary = profile.get("profile_summary")
        if summary:
            slim["profile_summary"] = summary
        elif profile.get("behavioural_metrics"):
            slim["profile_summary"] = json.dumps(
                profile.get("behavioural_metrics"), default=str
            )[:400]
    similar = entity.get("similar_entities") or []
    if similar:
        slim["similar_entities"] = [
            {
                "entity_id": s.get("entity_id"),
                "similarity": s.get("similarity"),
                "profile_summary": (s.get("profile_summary") or "")[:200],
                "risk_tier": s.get("risk_tier"),
            }
            for s in similar[:3]
        ]
    if entity.get("ml_feature_importances"):
        slim["ml_feature_importances"] = entity["ml_feature_importances"][:5]
    return slim


def _fallback_narratives(batch: list[dict], state: PipelineState) -> list[dict]:
    """Template narratives when LLM output cannot be parsed."""
    out: list[dict] = []
    for entity in batch:
        eid = entity.get("entity_id")
        if eid is None:
            continue
        signals = entity.get("signal_values") or {}
        top_signal = "unknown"
        if signals:
            top_signal = max(signals, key=lambda k: _narrative_signal_value(signals[k]))
        tier = entity.get("risk_tier", "high")
        score = entity.get("risk_score", 0)
        out.append({
            "entity_id": str(eid),
            "entity_name": entity.get("entity_name"),
            "risk_score": score,
            "risk_tier": tier,
            "risk_narrative": (
                f"Risk score {score:.2f} ({tier} tier). "
                f"Primary driver: {top_signal}."
            ),
        })
    return out


def _narrative_signal_value(v: object) -> float:
    try:
        return abs(float(v))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _augment_with_profile(entity: dict, profile: dict | None) -> dict:
    """Merge selected profiling fields into an entity payload for the LLM.

    The profile is only attached for in-memory narrative generation. Profile
    data is never persisted to the Pulse application database.
    """
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


def _profiles_from_scored_entities(entities: list[dict]) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    for entity in entities:
        signals = entity.get("signal_values") or {}
        top_signals = sorted(
            ((k, v) for k, v in signals.items() if isinstance(v, (int, float))),
            key=lambda item: abs(item[1]),
            reverse=True,
        )[:5]
        signal_text = ", ".join(f"{k}={v}" for k, v in top_signals)
        narrative = str(entity.get("risk_narrative") or "").strip()
        summary_parts = [
            f"{entity.get('entity_name') or entity.get('entity_id')} is {entity.get('risk_tier', 'unknown')} risk",
            f"score={entity.get('risk_score', 0)}",
        ]
        if signal_text:
            summary_parts.append(f"top signals: {signal_text}")
        if narrative:
            summary_parts.append(narrative)
        profiles.append({
            "entity_id": str(entity.get("entity_id", "")),
            "profile_summary": ". ".join(summary_parts),
            "behavioural_metrics": signals,
            "base_attributes": {
                "entity_name": entity.get("entity_name"),
                "risk_tier": entity.get("risk_tier"),
                "risk_score": entity.get("risk_score"),
                "scoring_method": entity.get("scoring_method"),
            },
        })
    return profiles
