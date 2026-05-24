"""Public API — Simulation endpoints.

Two endpoints that wrap the hackathon agents (now promoted to
``app/agents/workflows/``) and expose them under the standard public API
contract (X-API-Key auth, per-org scoping, Redis-backed rate limit, response
envelope).

For the live platform we expose **direct mode** for review simulation and
**cold-start mode** for recommendations — these are stateless and tenant-safe.
DB-mode (warm-start ``user_id``/``item_id`` against the Yelp slice) stays
available on the dedicated hackathon containers (``task-a-api:8011``,
``task-b-api:8012``) where the schema is fixed.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.agents.workflows.cold_start_recommender import ColdStartRecommendationAgent
from app.agents.workflows.review_simulator import ReviewSimulationAgent
from app.api.dependencies.api_key_auth import ApiKeyContext, require_api_key
from app.api.public.envelope import envelope
from app.api.public.schemas import PublicErrorResponse
from app.api.schemas.simulation import (
    RecommendRequest,
    RecommendResponse,
    SimulateReviewRequest,
    SimulateReviewResponse,
)

router = APIRouter(prefix="/simulation", tags=["Simulation"])
logger = logging.getLogger(__name__)

_ERRORS = {
    400: {"model": PublicErrorResponse, "description": "Invalid request (mode/inputs)"},
    401: {"model": PublicErrorResponse, "description": "Invalid or expired API key"},
    422: {"model": PublicErrorResponse, "description": "Agent could not produce valid JSON"},
    429: {"model": PublicErrorResponse, "description": "Rate limit exceeded"},
}


@router.post(
    "/review",
    response_model=None,  # public envelope wraps the SimulateReviewResponse body
    summary="Simulate a review for a persona × product",
    response_description="Predicted star rating, review text, and runtime meta.",
    responses=_ERRORS,
)
async def simulate_review(
    req: SimulateReviewRequest,
    ctx: ApiKeyContext = Depends(require_api_key("read")),
) -> dict:
    """Predict an authentic star rating (1-5) and 2-5 sentence review.

    Provide a ``persona`` object and a ``product`` object (direct mode). The
    ``user_id`` / ``item_id`` (DB mode) inputs are accepted by the schema but
    rejected at this endpoint — they only work on the hackathon containers.
    """
    if req.user_id or req.item_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=(
                "DB mode (user_id/item_id) is only available on the dedicated "
                "hackathon containers (task-a-api). This endpoint accepts persona + product only."
            ),
        )
    if not (req.persona and req.product):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Provide both `persona` and `product`.",
        )

    try:
        result = await ReviewSimulationAgent(register_db_tools=False).simulate(
            persona=req.persona.to_agent_dict(),
            product=req.product.to_agent_dict(),
            voice=req.voice,
        )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("simulate-review failed")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc

    return envelope(SimulateReviewResponse(**result).model_dump(), ctx.org_id)


@router.post(
    "/recommend",
    response_model=None,
    summary="Persona-driven recommendations (cold start, multi-turn, cross-domain)",
    response_description="Ranked recommendations with per-item rationale and runtime meta.",
    responses=_ERRORS,
)
async def recommend(
    req: RecommendRequest,
    ctx: ApiKeyContext = Depends(require_api_key("read")),
) -> dict:
    """Personalized recommendations from a free-text persona.

    Supports cold-start (``persona``), multi-turn refinement
    (``conversation_id`` + ``follow_up``), and cross-domain
    (``dataset=goodreads``). Warm-start (``user_id``) is accepted but only
    works on the hackathon containers where the Yelp slice is loaded.
    """
    if req.user_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=(
                "Warm-start by user_id is only available on the dedicated hackathon "
                "containers (task-b-api). This endpoint accepts a `persona` text only."
            ),
        )
    if not req.persona:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Provide a `persona` text.",
        )

    try:
        result = await ColdStartRecommendationAgent().recommend(
            user_id=None,
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

    return envelope(RecommendResponse(**result).model_dump(), ctx.org_id)
