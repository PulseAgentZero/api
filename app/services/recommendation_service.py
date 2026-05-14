from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.client_queries import (
    ClientDBError,
    compute_risk,
    fetch_entities,
    get_schema_mapping,
)
from app.infrastructure.database.repositories.recommendation_repository import (
    RecommendationRepository,
)


def _urgency_from_tier(tier: str) -> str:
    if tier == "critical":
        return "critical"
    if tier == "high":
        return "high"
    return "medium"


def _top_signal(entity: dict) -> str | None:
    signals = entity.get("signals") or {}
    if not signals:
        return None
    return max(signals, key=lambda key: signals[key])


async def generate_recommendations_for_org(
    db: AsyncSession,
    org_id: UUID,
    limit: int = 100,
) -> int:
    """Generate a hackathon-ready active recommendation queue from live risk scores."""

    mapping = await get_schema_mapping(db, org_id)
    entities = await fetch_entities(db, org_id, mapping)
    entities = compute_risk(entities, mapping.signal_columns, mapping.risk_config)
    at_risk = [
        entity
        for entity in sorted(entities, key=lambda item: item["risk_score"], reverse=True)
        if entity["risk_score"] >= 0.6
    ][:limit]

    repo = RecommendationRepository(db)
    existing = await repo.list_by_org(org_id, status="open")
    existing_entity_ids = {rec.entity_id for rec in existing}

    created = 0
    id_col = mapping.entity_id_col
    name_col = mapping.entity_name_col
    for entity in at_risk:
        entity_id = str(entity[id_col])
        if entity_id in existing_entity_ids:
            continue

        tier = entity["risk_tier"]
        top_signal = _top_signal(entity)
        entity_label = str(entity.get(name_col)) if name_col and entity.get(name_col) else None
        title = f"{tier.title()} risk intervention"
        reasoning = (
            f"{entity_id} is currently in the {tier} risk tier "
            f"with a score of {entity['risk_score']:.2f}."
        )
        if top_signal:
            reasoning += f" The strongest signal is {top_signal}."

        await repo.create(
            org_id=org_id,
            entity_id=entity_id,
            entity_label=entity_label,
            type="retention_intervention",
            urgency=_urgency_from_tier(tier),
            title=title,
            reasoning=reasoning,
            suggested_action=(
                "Review this entity's live profile and take the highest-fit "
                "retention action based on the mapped risk signals."
            ),
            status="open",
        )
        created += 1

    return created


__all__ = ["ClientDBError", "generate_recommendations_for_org"]
