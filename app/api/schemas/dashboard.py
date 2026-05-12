from pydantic import BaseModel


class RiskBreakdown(BaseModel):
    critical: int
    high: int
    medium: int
    low: int


class TopEntity(BaseModel):
    entity_id: str
    entity_label: str | None
    risk_score: float
    risk_tier: str


class OverviewResponse(BaseModel):
    total_entities: int
    risk_breakdown: RiskBreakdown
    top_at_risk: list[TopEntity]
    active_recommendations: int
