"""Server-Sent Event (SSE) wrappers for the hackathon agent endpoints.

The underlying ``BaseAgent`` ReAct loop is not currently incremental, so we
provide a *phased* streaming experience that still feels live to a curl
observer:

* ``event: start``      — agent run begins
* ``event: heartbeat``  — periodic ticks with ``elapsed_ms`` while the agent works
* ``event: result``     — full structured JSON response when the agent finishes
* ``event: token``      — final review / rationale text replayed word by word
* ``event: complete``   — stream done

Honest about the fact that token streaming here is a *replay*, not true LLM
streaming; the demo value is the visible progression of phases and tokens.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse

logger = logging.getLogger("hackathon.streaming")

SSE_MEDIA_TYPE = "text/event-stream"

# Cadence knobs — small enough to feel snappy, large enough to avoid spam.
_HEARTBEAT_INTERVAL_SECONDS = 0.5
_TOKEN_DELAY_SECONDS = 0.04


def _format(event: str, payload: dict[str, Any]) -> str:
    data = json.dumps(payload, default=str)
    return f"event: {event}\ndata: {data}\n\n"


async def sse_stream(
    *,
    task_name: str,
    agent_call: Callable[[], Awaitable[dict[str, Any]]],
    text_field: str,
) -> StreamingResponse:
    """Drive an agent call and emit phase + token SSE events around it."""

    async def generator():
        yield _format("start", {"task": task_name})
        started = time.perf_counter()
        run_task = asyncio.create_task(agent_call())

        try:
            try:
                while not run_task.done():
                    await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
                    yield _format(
                        "heartbeat",
                        {"elapsed_ms": int((time.perf_counter() - started) * 1000)},
                    )
                result = run_task.result()
            except HTTPException as exc:
                yield _format("error", {"status_code": exc.status_code, "message": exc.detail})
                return
            except Exception as exc:
                logger.exception("%s stream failed", task_name)
                yield _format("error", {"message": str(exc)})
                return

            yield _format("result", result)

            text = result.get(text_field, "") or ""
            for word in text.split():
                yield _format("token", {"token": word + " "})
                await asyncio.sleep(_TOKEN_DELAY_SECONDS)

            yield _format(
                "complete",
                {"duration_ms": int((time.perf_counter() - started) * 1000)},
            )
        finally:
            if not run_task.done():
                run_task.cancel()
                try:
                    await run_task
                except (asyncio.CancelledError, Exception):
                    pass

    return StreamingResponse(
        generator(),
        media_type=SSE_MEDIA_TYPE,
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering for true SSE
        },
    )


async def stream_simulate_review(req: Any) -> StreamingResponse:
    from hackathon.agents.review_simulator import ReviewSimulationAgent
    from hackathon.core.repository import fetch_item, fetch_user_profile

    if not ((req.persona and req.product) or (req.user_id and req.item_id)):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Provide (persona + product) for direct mode, or (user_id + item_id) for DB mode.",
        )

    async def call() -> dict[str, Any]:
        agent = ReviewSimulationAgent()
        if req.persona and req.product:
            return await agent.simulate(
                persona=req.persona.to_agent_dict(),
                product=req.product.to_agent_dict(),
                voice=req.voice,
            )
        profile, item = await asyncio.gather(
            fetch_user_profile(req.user_id or ""),
            fetch_item(req.item_id or ""),
        )
        if not profile:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"user {req.user_id} not found")
        if not item:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"item {req.item_id} not found")
        return await agent.simulate_from_context(
            user_id=req.user_id or "",
            item_id=req.item_id or "",
            persona=profile,
            product=item,
            voice=req.voice,
        )

    return await sse_stream(
        task_name="simulate-review", agent_call=call, text_field="text"
    )


async def stream_recommend(req: Any) -> StreamingResponse:
    from hackathon.agents.recommender import RecommendationAgent

    if not req.user_id and not req.persona:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Provide `user_id` (warm-start) or `persona` (cold-start).",
        )

    async def call() -> dict[str, Any]:
        agent = RecommendationAgent()
        return await agent.recommend(
            user_id=req.user_id,
            persona=req.persona,
            k=req.k,
            dataset=req.dataset,
            conversation_id=req.conversation_id,
            follow_up=req.follow_up,
        )

    # The recommend payload's prose lives in `rationale`; fall back to a synthesized
    # one-liner if the agent doesn't surface it.
    return await sse_stream(
        task_name="recommend", agent_call=call, text_field="rationale"
    )
