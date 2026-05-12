import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.schemas.alert import AlertResponse
from app.infrastructure.database.client_queries import (
    ClientDBError,
    compute_risk,
    fetch_entities,
    get_schema_mapping,
)
from app.infrastructure.database.models.user import User
from app.infrastructure.database.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("", response_model=list[AlertResponse])
async def list_alerts(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AlertResponse]:
    try:
        mapping = await get_schema_mapping(db, current_user.org_id)
        entities = await fetch_entities(db, current_user.org_id, mapping)
        entities = compute_risk(entities, mapping.signal_columns, mapping.risk_config)
    except ClientDBError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    id_col = mapping.entity_id_col
    name_col = mapping.entity_name_col

    critical = [e for e in entities if e["risk_tier"] == "critical"]
    return [
        AlertResponse(
            entity_id=e[id_col],
            entity_label=e.get(name_col) if name_col else None,
            risk_score=e["risk_score"],
            risk_tier=e["risk_tier"],
            reason=_alert_reason(e),
        )
        for e in critical
    ]


def _alert_reason(entity: dict) -> str:
    signals = entity.get("signals", {})
    if not signals:
        return "Critical risk score"
    worst = max(signals, key=lambda k: signals[k])
    return f"Elevated {worst}: {signals[worst]}"
