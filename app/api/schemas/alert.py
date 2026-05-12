from pydantic import BaseModel


class AlertResponse(BaseModel):
    entity_id: str
    entity_label: str | None
    risk_score: float
    risk_tier: str
    reason: str
