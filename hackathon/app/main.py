"""Combined hackathon gateway — exposes Task A and Task B in one container.

System (`/healthz`, `/readyz`, `/version`) and demo helpers (`/stats`,
`/samples/users`, `/samples/items`, `/users/{id}`, `/items/{id}`, `/search`,
`/metrics`) live in :mod:`hackathon.app.system_router` and are mounted by
:func:`hackathon.app.factory.create_app`. This module adds the two task POST
endpoints on top.
"""

from __future__ import annotations

import logging
import asyncio

from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse

from hackathon.agents.recommender import RecommendationAgent
from hackathon.agents.review_simulator import ReviewSimulationAgent
from hackathon.app.factory import configure_logging, create_app
from hackathon.app.schemas import (
    RecommendRequest,
    RecommendResponse,
    SimulateReviewRequest,
    SimulateReviewResponse,
)
from hackathon.app.streaming import stream_recommend, stream_simulate_review
from hackathon.core.repository import fetch_item, fetch_user_profile

configure_logging()
logger = logging.getLogger("hackathon.api")

TAG_TASK_A = "Task A — Review simulation"
TAG_TASK_B = "Task B — Recommendations"

app = create_app(
    title="Entivia — DSN x Bluechip LLM Agent Challenge",
    description=(
        "A containerized FastAPI demo for the DSN x Bluechip LLM Agent Challenge.\n\n"
        "## What it demonstrates\n"
        "- **Task A — Review simulation:** accepts **persona + product** (direct mode) or "
        "`user_id` + `item_id` (DB demo); returns star rating + review text.\n"
        "- **Two submission containers:** Task A `:8011`, Task B `:8012` (this gateway `:8010` "
        "exposes both).\n"
        "- **Task B — Recommendations:** combines Voyage `voyage-4-large` embeddings "
        "(1024-d) in Qdrant with Entivia's `BaseAgent` for warm-start, cold-start, "
        "multi-turn, and cross-domain recommendations — the same retrieval stack "
        "running in Entivia production.\n"
        "- **Measurable agents:** every response includes runtime `meta`, and "
        "`GET /metrics` exposes the latest evaluation report.\n"
        "- **Observability:** `GET /healthz` reports DB / Qdrant / LLM / embedding status; "
        "`GET /readyz` is a strict 503-on-failure probe.\n\n"
        "Start with `GET /stats` and `GET /samples/users`, then try `POST /simulate-review` "
        "and `POST /recommend` using the examples in Swagger."
    ),
    task_id="combined",
    extra_tags=[
        {"name": TAG_TASK_A, "description": "Predict stars and review text for a user × item pair."},
        {"name": TAG_TASK_B, "description": "Personalized recommendations across cold/warm/cross-domain modes."},
    ],
)


@app.post(
    "/simulate-review",
    response_model=SimulateReviewResponse,
    summary="Simulate a user's review for an item",
    tags=[TAG_TASK_A],
    description=(
        "Runs the ReviewSimulationAgent. **Direct mode:** send `persona` + `product` "
        "objects (challenge spec). **DB mode:** send `user_id` + `item_id` to load "
        "profile and item from Postgres before the LLM call (faster than tool-loop "
        "DB mode). Returns stars, text, and runtime `meta`. Use `voice=nigerian` "
        "for the localization bonus."
    ),
)
async def simulate_review(req: SimulateReviewRequest) -> SimulateReviewResponse:
    agent = ReviewSimulationAgent()
    try:
        if req.persona and req.product:
            result = await agent.simulate(
                persona=req.persona.to_agent_dict(),
                product=req.product.to_agent_dict(),
                voice=req.voice,
            )
        else:
            profile, item = await asyncio.gather(
                fetch_user_profile(req.user_id or ""),
                fetch_item(req.item_id or ""),
            )
            if not profile:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"user {req.user_id} not found")
            if not item:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"item {req.item_id} not found")
            result = await agent.simulate_from_context(
                user_id=req.user_id or "",
                item_id=req.item_id or "",
                persona=profile,
                product=item,
                voice=req.voice,
            )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.warning("simulate-review failed: %s", exc)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("simulate-review crashed")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    return SimulateReviewResponse(**result)


@app.post(
    "/recommend",
    response_model=RecommendResponse,
    summary="Generate personalized recommendations",
    tags=[TAG_TASK_B],
    description=(
        "Runs the RecommendationAgent. For warm-start users, it builds a persona "
        "vector from real review history and excludes already-reviewed items. For "
        "cold-start, it embeds the free-text persona. Qdrant retrieves candidates; "
        "the LLM reranks them and explains each recommendation. Pass a returned "
        "`conversation_id` plus `follow_up` for multi-turn refinement. Use "
        "`dataset=goodreads` to demonstrate cross-domain recommendations."
    ),
)
async def recommend(req: RecommendRequest) -> RecommendResponse:
    if not req.user_id and not req.persona:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Provide either `user_id` (warm-start) or `persona` (cold-start).",
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
        logger.exception("recommend crashed")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    return RecommendResponse(**result)


@app.post(
    "/simulate-review/stream",
    summary="Stream a review simulation via Server-Sent Events",
    tags=[TAG_TASK_A],
    description=(
        "Phased SSE: `start` -> `heartbeat` (0.5s ticks) -> `result` (full JSON) "
        "-> `token` (review text replayed word by word) -> `complete`. Curl: "
        "`curl -N -X POST .../simulate-review/stream -H 'content-type: application/json' -d '<payload>'`."
    ),
    response_class=StreamingResponse,
)
async def simulate_review_stream(req: SimulateReviewRequest):
    return await stream_simulate_review(req)


@app.post(
    "/recommend/stream",
    summary="Stream personalized recommendations via Server-Sent Events",
    tags=[TAG_TASK_B],
    description=(
        "Phased SSE around `/recommend`: `start` -> `heartbeat` -> `result` "
        "(ranked list JSON) -> `token` (rationale replayed word by word) -> `complete`."
    ),
    response_class=StreamingResponse,
)
async def recommend_stream(req: RecommendRequest):
    return await stream_recommend(req)
