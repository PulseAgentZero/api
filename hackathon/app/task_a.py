"""Task A — Review Simulation Agent (standalone container)."""

from __future__ import annotations

import logging

from fastapi import HTTPException, Query, status

from hackathon.agents.review_simulator import ReviewSimulationAgent
from hackathon.app.factory import configure_logging, create_app
from hackathon.app.schemas import (
    HealthResponse,
    MetricsResponse,
    SampleUsersResponse,
    SimulateReviewRequest,
    SimulateReviewResponse,
)
from hackathon.core.repository import list_sample_user_ids
from hackathon.eval.metrics import load_eval_snapshot

configure_logging()
logger = logging.getLogger("hackathon.task_a")

app = create_app(
    title="Entivia — Task A: Review Simulation Agent",
    description=(
        "Containerized **Task A** submission for the DSN × Bluechip LLM Agent Challenge.\n\n"
        "## Input (challenge spec)\n"
        "Provide **user persona + product details** and receive a predicted **star rating** "
        "and **review text**.\n\n"
        "## Modes\n"
        "- **Direct** — `POST /simulate-review` with `persona` + `product` JSON objects "
        "(no database required).\n"
        "- **DB demo** — same endpoint with `user_id` + `item_id` from the loaded Yelp slice; "
        "use `GET /samples/users` to discover ids.\n\n"
        "Every response includes runtime `meta`. Latest offline eval: `GET /metrics`."
    ),
)


@app.get("/healthz", response_model=HealthResponse, summary="Health check")
async def healthz() -> HealthResponse:
    return HealthResponse()


@app.get("/samples/users", response_model=SampleUsersResponse, summary="Sample user ids (DB mode)")
async def sample_users(
    dataset: str = Query("yelp"),
    limit: int = Query(5, ge=1, le=50),
) -> SampleUsersResponse:
    return SampleUsersResponse(dataset=dataset, user_ids=await list_sample_user_ids(dataset, limit))


@app.get("/metrics", response_model=MetricsResponse, summary="Latest Task A evaluation metrics")
async def metrics() -> MetricsResponse:
    return MetricsResponse(**load_eval_snapshot().to_dict())


@app.post(
    "/simulate-review",
    response_model=SimulateReviewResponse,
    summary="Simulate review from persona + product",
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
            result = await agent.simulate(
                user_id=req.user_id,
                item_id=req.item_id,
                voice=req.voice,
            )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("simulate-review failed")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    return SimulateReviewResponse(**result)
