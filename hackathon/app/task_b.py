"""Task B — Recommendation Agent (standalone container)."""

from __future__ import annotations

import logging

from fastapi import HTTPException, status

from hackathon.agents.recommender import RecommendationAgent
from hackathon.app.factory import configure_logging, create_app
from hackathon.app.schemas import (
    HealthResponse,
    MetricsResponse,
    RecommendRequest,
    RecommendResponse,
)
from hackathon.eval.metrics import load_eval_snapshot

configure_logging()
logger = logging.getLogger("hackathon.task_b")

app = create_app(
    title="Entivia — Task B: Recommendation Agent",
    description=(
        "Containerized **Task B** submission for the DSN × Bluechip LLM Agent Challenge.\n\n"
        "## Input\n"
        "Provide a **user persona** (cold-start text or warm-start `user_id` from the Yelp "
        "persona dataset built in Task A) and receive **personalized recommendations**.\n\n"
        "## Features\n"
        "- Warm-start (`user_id`) and cold-start (`persona`)\n"
        "- Multi-turn refinement (`conversation_id` + `follow_up`)\n"
        "- Cross-domain (`dataset=goodreads`)\n"
        "- Voyage voyage-4-large embeddings (1024-d) + Qdrant ANN + LLM reranking\n\n"
        "Requires Postgres + Qdrant (see `docker compose`). Every response includes `meta`. "
        "Offline eval: `GET /metrics`."
    ),
)


@app.get("/healthz", response_model=HealthResponse, summary="Health check")
async def healthz() -> HealthResponse:
    return HealthResponse()


@app.get("/metrics", response_model=MetricsResponse, summary="Latest Task B evaluation metrics")
async def metrics() -> MetricsResponse:
    return MetricsResponse(**load_eval_snapshot().to_dict())


@app.post("/recommend", response_model=RecommendResponse, summary="Personalized recommendations")
async def recommend(req: RecommendRequest) -> RecommendResponse:
    if not req.user_id and not req.persona:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Provide `user_id` (warm-start) or `persona` (cold-start).",
        )
    try:
        result = await RecommendationAgent().recommend(
            user_id=req.user_id,
            persona=req.persona,
            k=req.k,
            dataset=req.dataset,
            conversation_id=req.conversation_id,
            follow_up=req.follow_up,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("recommend failed")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    return RecommendResponse(**result)
