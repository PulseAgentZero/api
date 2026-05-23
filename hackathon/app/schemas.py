"""Pydantic models for the hackathon FastAPI surface.

The agent-level contracts (persona, product, review, recommend, runtime meta)
have been promoted to :mod:`app.api.schemas.simulation` so the production
public API and the hackathon containers share one source of truth. The
hackathon-only demo models (health, sample users, metrics, eval records)
stay here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

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
    "SampleUsersResponse",
    "EvalTaskARecord",
    "EvalBaselineRecord",
    "EvalTaskBRecord",
    "MetricsResponse",
]


class HealthResponse(BaseModel):
    ok: bool = True
    engine: Literal["entivia"] = "entivia"


class SampleUsersResponse(BaseModel):
    dataset: str
    user_ids: list[str]


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
