"""Pydantic models for the hackathon FastAPI surface.

The agent-level contracts (persona, product, review, recommend, runtime meta)
have been promoted to :mod:`app.api.schemas.simulation` so the production
public API and the hackathon containers share one source of truth. The
hackathon-only demo models (health, sample users, metrics, eval records)
stay here.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.api.schemas.simulation import (
    AgentRunMeta,
    PersonaInput,
    ProductInput,
    RecommendItem,
    RecommendRequest,
    RecommendResponse,
    SimulateReviewRequest,
    SimulateReviewResponse,
)

__all__ = [
    "AgentRunMeta",
    "PersonaInput",
    "ProductInput",
    "RecommendItem",
    "RecommendRequest",
    "RecommendResponse",
    "SimulateReviewRequest",
    "SimulateReviewResponse",
    "HealthResponse",
    "ReadinessResponse",
    "VersionResponse",
    "StatsResponse",
    "DatasetStats",
    "SampleUsersResponse",
    "SampleItemsResponse",
    "UserProfileResponse",
    "ItemResponse",
    "SearchHit",
    "SearchResponse",
    "ItemSimilarResponse",
    "PredictRatingRequest",
    "PredictRatingResponse",
    "AgentToolParamDescriptor",
    "AgentToolDescriptor",
    "AgentDescriptor",
    "AgentToolsResponse",
    "ConversationResponse",
    "CompareRequest",
    "ProviderRun",
    "CompareResponse",
    "EvalTaskARecord",
    "EvalBaselineRecord",
    "EvalTaskBRecord",
    "MetricsResponse",
]


# ── System ────────────────────────────────────────────────────────────────────

HealthStatus = Literal["healthy", "degraded", "unhealthy"]


class DatabaseCheck(BaseModel):
    configured: bool
    ok: bool
    latency_ms: float | None = None
    users: int | None = None
    items: int | None = None
    reviews: int | None = None
    error: str | None = None


class QdrantCheck(BaseModel):
    configured: bool
    ok: bool
    latency_ms: float | None = None
    collection: str
    points: int | None = None
    vector_size: int | None = None
    error: str | None = None


class LLMCheck(BaseModel):
    primary: Literal["anthropic", "groq"]
    anthropic_configured: bool
    groq_configured: bool
    fallback_ready: bool = Field(
        ..., description="True when at least one provider key is set."
    )


class EmbeddingsCheck(BaseModel):
    backend: str
    model: str
    vector_size: int
    credentials_configured: bool
    pseudo_mode: bool


class HealthChecks(BaseModel):
    database: DatabaseCheck
    qdrant: QdrantCheck
    llm: LLMCheck
    embeddings: EmbeddingsCheck


class HealthResponse(BaseModel):
    """Enriched liveness + dependency status.

    Always returns HTTP 200; the embedded ``status`` field describes overall
    health. Use ``GET /readyz`` for a strict 503-on-failure probe.
    """

    status: HealthStatus = "healthy"
    engine: Literal["entivia"] = "entivia"
    task: Literal["task_a", "task_b", "combined"] = "combined"
    uptime_seconds: float = 0.0
    checks: HealthChecks
    version: dict[str, str] = Field(default_factory=dict)


class ReadinessResponse(BaseModel):
    ready: bool
    failing: list[str] = Field(default_factory=list)
    detail: str | None = None


class VersionResponse(BaseModel):
    image: str
    build: str
    task: Literal["task_a", "task_b", "combined"]
    embedding_backend: str
    embedding_model: str
    vector_size: int
    primary_llm: Literal["anthropic", "groq"]
    llm_model_override: str | None = Field(
        None,
        description="Optional HACKATHON_LLM_MODEL override used by hackathon agents.",
    )
    qdrant_collection: str


# ── Demo ──────────────────────────────────────────────────────────────────────


class DatasetStats(BaseModel):
    dataset: str
    users: int
    items: int
    reviews: int
    holdout_reviews: int


class StatsResponse(BaseModel):
    datasets: list[DatasetStats]
    users: int
    items: int
    reviews: int
    holdout_reviews: int
    qdrant_points: int | None = None


class SampleUsersResponse(BaseModel):
    dataset: str
    user_ids: list[str]


class SampleItemsResponse(BaseModel):
    dataset: str
    items: list[dict[str, Any]] = Field(
        ..., description="Each item carries id and name to make demos copy-paste friendly."
    )


class UserSampleReview(BaseModel):
    stars: float
    text: str
    item_name: str


class UserProfileResponse(BaseModel):
    user_id: str
    dataset: str
    avg_stars: float | None = None
    n_reviews: int | None = None
    top_categories: list[str] = Field(default_factory=list)
    sample_reviews: list[UserSampleReview] = Field(default_factory=list)


class ItemResponse(BaseModel):
    item_id: str
    dataset: str
    name: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchHit(BaseModel):
    item_id: str
    name: str
    dataset: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    query: str
    dataset: str | None = None
    hits: list[SearchHit]


# ── Standout endpoints ────────────────────────────────────────────────────────


class ItemSimilarResponse(BaseModel):
    item_id: str
    name: str
    dataset: str
    hits: list[SearchHit] = Field(
        ..., description="Items most similar to the seed item (excluding the seed itself)."
    )


class PredictRatingRequest(BaseModel):
    """Optional payload for `/predict-rating`.

    Three valid shapes:

    - Empty body — defaults to DB mode, requires `user_id` from the URL path.
      In that case `item_id` must be set (`{"item_id": "..."}`).
    - `{"persona": {...}, "product": {...}}` — direct mode.
    - `{"item_id": "..."}` — DB mode (uses the `user_id` from the URL path).

    Partial payloads (e.g. `persona` without `product`) are rejected to avoid
    silently dropping fields the caller meant to supply.
    """

    user_id: str | None = None
    item_id: str | None = None
    persona: PersonaInput | None = None
    product: ProductInput | None = None

    @model_validator(mode="after")
    def _check_modes(self) -> "PredictRatingRequest":
        if (self.persona is None) ^ (self.product is None):
            raise ValueError(
                "`persona` and `product` must be provided together (direct mode), or "
                "both omitted (DB mode uses `item_id`)."
            )
        return self


class PredictRatingResponse(BaseModel):
    user_id: str | None = None
    item_id: str | None = None
    predicted_stars: float
    confidence: float | None = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Approximate 0-1 confidence derived from the underlying agent meta.",
    )
    meta: AgentRunMeta


class AgentToolParamDescriptor(BaseModel):
    name: str
    type: str
    description: str
    required: bool
    enum: list[str] | None = None


class AgentToolDescriptor(BaseModel):
    name: str
    description: str
    parameters: list[AgentToolParamDescriptor]


class AgentDescriptor(BaseModel):
    agent: Literal["review_simulator", "recommender"]
    provider: Literal["anthropic", "groq"]
    fallback_enabled: bool
    tools: list[AgentToolDescriptor]


class AgentToolsResponse(BaseModel):
    agents: list[AgentDescriptor]


class ConversationResponse(BaseModel):
    conversation_id: str
    dataset: str | None = None
    shown_ids: list[str] = Field(default_factory=list)
    turns: int = Field(..., description="Number of recommend turns seen so far.")


class CompareRequest(BaseModel):
    kind: Literal["simulate-review", "recommend"] = Field(
        ..., description="Which task to compare across providers."
    )
    simulate_review: SimulateReviewRequest | None = None
    recommend: RecommendRequest | None = None


class ProviderRun(BaseModel):
    provider: Literal["anthropic", "groq"]
    ok: bool
    duration_ms: int | None = None
    response: dict[str, Any] | None = None
    error: str | None = None


class CompareResponse(BaseModel):
    kind: Literal["simulate-review", "recommend"]
    runs: list[ProviderRun]


class EvalTaskARecord(BaseModel):
    voice: str
    n: int
    rmse: float
    rouge_l: float
    bert_f1: float | None = None


class EvalBaselineRecord(BaseModel):
    mode: str
    n: int
    rmse: float


class EvalTaskBRecord(BaseModel):
    mode: str
    n: int
    k: int
    hit_at_k: float
    ndcg_at_k: float | None = None


class MetricsResponse(BaseModel):
    available: bool = Field(..., description="False until `hackathon.eval.run` has produced EVAL.md.")
    path: str = Field(..., description="Path to the Markdown eval artifact inside the container.")
    generated_from: str = Field(..., description="Human-readable source/note from the report.")
    task_a: list[EvalTaskARecord] = Field(default_factory=list)
    baselines: list[EvalBaselineRecord] = Field(default_factory=list)
    task_b: list[EvalTaskBRecord] = Field(default_factory=list)
