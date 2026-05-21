"""Persist scored entities to entity_profiles and entity_risk_history."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.state import PipelineState
from app.infrastructure.database.models.entity_profile import EntityProfile
from app.infrastructure.database.models.entity_risk_history import EntityRiskHistory

logger = logging.getLogger(__name__)


def _display_tier(raw: str) -> str:
    m = {"critical": "High", "high": "High", "medium": "Medium", "low": "Healthy"}
    return m.get(str(raw).lower(), str(raw).title())


def _sanitize_json_value(value: Any) -> Any:
    """Recursively convert Decimal to float for JSONB storage."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _sanitize_json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_json_value(v) for v in value]
    return value


async def persist_entity_profiles_from_pipeline(
    session: AsyncSession,
    *,
    org_id: UUID,
    run_id: UUID,
    mapping_id: UUID,
    state: PipelineState,
) -> None:
    scored = state.get("scored_entities") or []
    if not scored:
        return

    # Only one mapping's profiles should be latest per org (avoids inflated chat counts).
    await session.execute(
        update(EntityProfile)
        .where(
            EntityProfile.org_id == org_id,
            EntityProfile.is_latest.is_(True),
        )
        .values(is_latest=False)
    )

    for e in scored:
        tier = _display_tier(e.get("risk_tier", "low"))
        score = Decimal(str(round(float(e.get("risk_score", 0)), 3)))
        prof = EntityProfile(
            org_id=org_id,
            pipeline_run_id=run_id,
            mapping_id=mapping_id,
            entity_id=str(e.get("entity_id", "")),
            entity_name=e.get("entity_name"),
            segment=None,
            profile_data={"signal_values": _sanitize_json_value(e.get("signal_values", {}))},
            risk_score=score,
            risk_tier=tier,
            risk_narrative=e.get("risk_narrative"),
            is_latest=True,
        )
        session.add(prof)
        session.add(
            EntityRiskHistory(
                org_id=org_id,
                pipeline_run_id=run_id,
                entity_id=str(e.get("entity_id", "")),
                risk_score=score,
                risk_tier=tier,
            )
        )
    logger.info("[Pipeline] Persisted %d entity profiles for org %s", len(scored), org_id)
