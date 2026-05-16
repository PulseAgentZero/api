from __future__ import annotations

from pydantic import BaseModel


class EntityListItem(BaseModel):
    entity_id: str
    entity_name: str | None
    segment: str | None
    risk_score: float
    risk_tier: str | None
    risk_narrative: str | None
    open_recommendations: int
    created_at: str


class EntityListResponse(BaseModel):
    entities: list[EntityListItem]
    total: int
    page: int
    limit: int
    pages: int


class RecommendationSummary(BaseModel):
    id: str
    title: str | None
    urgency: str | None
    status: str | None
    created_at: str


class EntityDetail(BaseModel):
    entity_id: str
    entity_name: str | None
    segment: str | None
    risk_score: float
    risk_tier: str | None
    risk_narrative: str | None
    profile_data: dict
    recommendations: list[RecommendationSummary]
    last_pipeline_run_at: str


class RiskHistoryPoint(BaseModel):
    risk_score: float
    risk_tier: str | None
    recorded_at: str


class EntityRiskHistoryResponse(BaseModel):
    entity_id: str
    period: str
    points: list[RiskHistoryPoint]
