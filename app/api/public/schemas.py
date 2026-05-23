"""Pydantic models for the Public API OpenAPI schema (ReDoc / Swagger)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# ── Shared envelope ───────────────────────────────────────────────────────────

class PublicMeta(BaseModel):
    """Metadata included on every authenticated public API response."""

    org_id: str = Field(
        ...,
        description="UUID of the organization tied to your API key. All data is scoped to this org.",
        examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
    )
    api_version: str = Field(
        "1",
        description="Public API version identifier.",
    )


class PublicErrorDetail(BaseModel):
    code: str = Field(
        ...,
        description="Machine-readable error code (e.g. `INVALID_API_KEY`, `NOT_FOUND`, `RATE_LIMITED`).",
        examples=["INVALID_API_KEY"],
    )
    message: str = Field(..., description="Human-readable explanation of the error.")
    fields: dict[str, str] | None = Field(
        None,
        description="Field-level validation errors, when applicable.",
    )


class PublicErrorResponse(BaseModel):
    error: PublicErrorDetail


# ── Entities ──────────────────────────────────────────────────────────────────

RiskTier = Literal["High", "Medium", "Low", "Healthy"]
RiskPeriod = Literal["7d", "30d", "90d", "180d"]


class EntitySummary(BaseModel):
    entity_id: str = Field(..., description="Stable identifier from your connected database.")
    entity_name: str | None = Field(None, description="Display name when available.")
    segment: str | None = Field(None, description="Business segment or cohort label.")
    risk_score: float = Field(..., description="Latest composite risk score (0–100).")
    risk_tier: RiskTier | None = None
    risk_narrative: str | None = Field(None, description="Short LLM-generated risk summary.")
    open_recommendations: int = Field(0, description="Count of open recommendations for this entity.")
    created_at: str = Field(..., description="ISO 8601 timestamp of the latest profile snapshot.")


class EntityListData(BaseModel):
    entities: list[EntitySummary]
    total: int = Field(..., description="Total entities matching filters (all pages).")
    page: int
    limit: int
    pages: int = Field(..., description="Total number of pages at the current `limit`.")


class EntityRecommendationStub(BaseModel):
    id: str
    title: str
    urgency: str | None = None
    status: str
    created_at: str


class EntityDetailData(BaseModel):
    entity_id: str
    entity_name: str | None = None
    segment: str | None = None
    risk_score: float
    risk_tier: RiskTier | None = None
    risk_narrative: str | None = None
    profile_data: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured behavioral profile fields produced by the pipeline.",
    )
    recommendations: list[EntityRecommendationStub] = Field(
        ...,
        description="Up to 20 most recent recommendations for this entity.",
    )
    last_pipeline_run_at: str = Field(
        ...,
        description="ISO 8601 timestamp when this profile was last refreshed.",
    )


class RiskHistoryPoint(BaseModel):
    risk_score: float
    risk_tier: RiskTier | None = None
    recorded_at: str = Field(..., description="ISO 8601 timestamp.")


class EntityRiskHistoryData(BaseModel):
    entity_id: str
    period: RiskPeriod
    points: list[RiskHistoryPoint]


class EntityListResponse(BaseModel):
    data: EntityListData
    meta: PublicMeta


class EntityDetailResponse(BaseModel):
    data: EntityDetailData
    meta: PublicMeta


class EntityRiskHistoryResponse(BaseModel):
    data: EntityRiskHistoryData
    meta: PublicMeta


# ── Recommendations ───────────────────────────────────────────────────────────

RecStatus = Literal["open", "actioned", "dismissed", "snoozed", "escalated"]
RecUrgency = Literal["critical", "high", "medium", "low"]


class RecommendationRecord(BaseModel):
    id: str
    entity_id: str
    entity_label: str | None = None
    type: str | None = Field(None, description="Recommendation category (e.g. retention, upsell).")
    title: str
    urgency: RecUrgency | str | None = None
    confidence_score: float | None = Field(None, ge=0, le=1)
    reasoning: str | None = None
    suggested_action: str | None = None
    expected_impact: str | None = None
    status: RecStatus | str
    expires_at: str | None = Field(None, description="ISO 8601 expiry, if set.")
    created_at: str


class RecommendationListData(BaseModel):
    recommendations: list[RecommendationRecord]
    total: int
    page: int
    limit: int


class ActionRecommendationRequest(BaseModel):
    outcome_notes: str | None = Field(
        None,
        max_length=4000,
        description="Optional notes describing the action taken (stored on the recommendation).",
    )


class DismissRecommendationRequest(BaseModel):
    reason: str | None = Field(
        None,
        max_length=2000,
        description="Optional reason the recommendation was dismissed.",
    )


class RecommendationListResponse(BaseModel):
    data: RecommendationListData
    meta: PublicMeta


class RecommendationDetailResponse(BaseModel):
    data: RecommendationRecord
    meta: PublicMeta


# ── Pipeline ──────────────────────────────────────────────────────────────────

class PipelineTriggerRequest(BaseModel):
    mapping_id: str | None = Field(
        None,
        description=(
            "Optional schema-mapping UUID. When omitted, the org's default mapping is used. "
            "Use this to run the pipeline against a specific entity configuration."
        ),
        examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
    )


class PipelineTriggerData(BaseModel):
    run_id: str = Field(..., description="UUID of the queued pipeline run.")
    status: Literal["queued"] = "queued"
    message: str = Field(..., examples=["Pipeline queued"])


class PipelineRunRecord(BaseModel):
    id: str
    org_id: str
    status: str = Field(..., description="queued | running | completed | failed")
    trigger_source: str | None = None
    triggered_by: str | None = Field(None, description="User UUID when triggered from the app; null for API keys.")
    mapping_id: str | None = None
    current_step: str | None = Field(None, description="Active agent step while running.")
    error: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = None
    entities_scored: int | None = None
    critical_count: int | None = None
    high_count: int | None = None
    recommendations_generated: int | None = None
    total_llm_calls: int | None = None
    total_tool_calls: int | None = None
    total_tokens: int | None = None
    provider_fallbacks: int | None = None
    created_at: str | None = None


class PipelineRunListData(BaseModel):
    runs: list[PipelineRunRecord]


class PipelineTriggerResponse(BaseModel):
    data: PipelineTriggerData
    meta: PublicMeta


class PipelineRunListResponse(BaseModel):
    data: PipelineRunListData
    meta: PublicMeta


# ── Analytics ─────────────────────────────────────────────────────────────────

AnalyticsPeriod = Literal["7d", "30d", "90d"]


class RiskDistribution(BaseModel):
    High: int = 0
    Medium: int = 0
    Low: int = 0
    Healthy: int = 0


class AnalyticsOverviewData(BaseModel):
    period: AnalyticsPeriod | str
    total_entities: int
    risk_distribution: RiskDistribution
    average_risk_score: float | None = None
    open_recommendations: int
    pipeline_runs_in_period: int = Field(
        ...,
        description="Number of pipeline runs started within the selected period.",
    )


class AnalyticsOverviewResponse(BaseModel):
    data: AnalyticsOverviewData
    meta: PublicMeta
