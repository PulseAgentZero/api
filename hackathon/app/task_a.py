"""Task A — Review Simulation Agent (standalone container)."""

from __future__ import annotations

import logging
import asyncio

from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse

from hackathon.agents.review_simulator import ReviewSimulationAgent
from hackathon.app.factory import configure_logging, create_app
from hackathon.app.schemas import SimulateReviewRequest, SimulateReviewResponse
from hackathon.app.streaming import stream_simulate_review
from hackathon.core.repository import fetch_item, fetch_user_profile

configure_logging()
logger = logging.getLogger("hackathon.task_a")

TAG_TASK_A = "Task A — Review simulation"

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
        "use `GET /samples/users` and `GET /samples/items` to discover ids. DB mode "
        "prefetches profile/item context before the LLM call for lower latency.\n\n"
        "Every response includes runtime `meta`. Latest offline eval: `GET /metrics`. "
        "Dependency status: `GET /healthz` (rich) and `GET /readyz` (strict)."
    ),
    task_id="task_a",
    extra_tags=[{"name": TAG_TASK_A, "description": "Predict stars and review text."}],
)


@app.post(
    "/simulate-review",
    response_model=SimulateReviewResponse,
    summary="Simulate review from persona + product",
    tags=[TAG_TASK_A],
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
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("simulate-review failed")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    return SimulateReviewResponse(**result)


@app.post(
    "/simulate-review/stream",
    summary="Simulate review as a Server-Sent Event stream",
    tags=[TAG_TASK_A],
    description=(
        "Same input as `/simulate-review`, but streamed as SSE: `start` -> "
        "`heartbeat` (every 0.5s while the agent works) -> `result` (full JSON) "
        "-> `token` (final review text replayed word by word) -> `complete`. "
        "Try with: `curl -N -X POST .../simulate-review/stream -H 'content-type: application/json' -d '<payload>'`."
    ),
    response_class=StreamingResponse,
)
async def simulate_review_stream(req: SimulateReviewRequest):
    return await stream_simulate_review(req)
