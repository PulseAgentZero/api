"""Task B — Recommendation Agent (standalone container)."""

from __future__ import annotations

import logging

from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse

from hackathon.agents.recommender import RecommendationAgent
from hackathon.app.factory import configure_logging, create_app
from hackathon.app.schemas import RecommendRequest, RecommendResponse
from hackathon.app.streaming import stream_recommend

configure_logging()
logger = logging.getLogger("hackathon.task_b")

TAG_TASK_B = "Task B — Recommendations"

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
        "Offline eval: `GET /metrics`. Dependency status: `GET /healthz` and `GET /readyz`."
    ),
    task_id="task_b",
    extra_tags=[{"name": TAG_TASK_B, "description": "Personalized recommendations."}],
)


@app.post(
    "/recommend",
    response_model=RecommendResponse,
    summary="Personalized recommendations",
    tags=[TAG_TASK_B],
)
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


@app.post(
    "/recommend/stream",
    summary="Recommend as a Server-Sent Event stream",
    tags=[TAG_TASK_B],
    description=(
        "Same input as `/recommend`, but streamed as SSE: `start` -> "
        "`heartbeat` (every 0.5s while the agent works) -> `result` (full JSON) "
        "-> `token` (rationale replayed word by word) -> `complete`."
    ),
    response_class=StreamingResponse,
)
async def recommend_stream(req: RecommendRequest):
    return await stream_recommend(req)
