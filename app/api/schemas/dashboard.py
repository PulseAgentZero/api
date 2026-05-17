from __future__ import annotations

from pydantic import BaseModel


class RiskDistribution(BaseModel):
    High: int
    Medium: int
    Low: int
    Healthy: int


class TopAtRiskEntity(BaseModel):
    entity_id: str
    entity_name: str | None
    risk_score: float
    risk_tier: str | None
    segment: str | None


class LastPipelineRun(BaseModel):
    id: str
    status: str
    completed_at: str | None
    entities_scored: int | None


class RiskTrendPoint(BaseModel):
    date: str
    avg_risk_score: float
    count: int


class DashboardOverviewResponse(BaseModel):
    total_entities: int
    total_entities_change_pct: float | None
    risk_distribution: RiskDistribution
    risk_distribution_prev: RiskDistribution
    high_risk_change_pct: float | None
    top_at_risk: list[TopAtRiskEntity]
    active_recommendations: int
    critical_recommendations: int
    last_pipeline_run: LastPipelineRun | None
    risk_trend: list[RiskTrendPoint]
