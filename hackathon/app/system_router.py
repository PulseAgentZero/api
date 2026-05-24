"""Shared system + demo routers mounted by every hackathon FastAPI app.

Separates cross-cutting endpoints (health, readiness, version, stats, samples,
profile lookups, vector search, metrics) from the task-specific entry points so
the three apps (``task_a``, ``task_b``, combined ``main``) stay focused on the
work they exist to do.

The routers are intentionally read-only and depend only on the same Postgres /
Qdrant / LLM configuration the task endpoints already require — no new
credentials.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import asyncio

from fastapi import APIRouter, Body, HTTPException, Query, Request, Response, status

from hackathon.app.schemas import (
    AgentDescriptor,
    AgentToolDescriptor,
    AgentToolParamDescriptor,
    AgentToolsResponse,
    CompareRequest,
    CompareResponse,
    ConversationResponse,
    DatabaseCheck,
    DatasetStats,
    EmbeddingsCheck,
    HealthChecks,
    HealthResponse,
    ItemResponse,
    ItemSimilarResponse,
    LLMCheck,
    MetricsResponse,
    PredictRatingRequest,
    PredictRatingResponse,
    ProviderRun,
    QdrantCheck,
    ReadinessResponse,
    SampleItemsResponse,
    SampleUsersResponse,
    SearchHit,
    SearchResponse,
    StatsResponse,
    UserProfileResponse,
    VersionResponse,
)
from app.config.settings import settings
from hackathon.config import (
    EMBEDDING_BACKEND,
    FASTEMBED_MODEL,
    HACKATHON_DATABASE_URL,
    HACKATHON_LLM_MODEL,
    HACKATHON_LLM_PROVIDER,
    HACKATHON_QDRANT_COLLECTION,
    USE_PSEUDO_EMBEDDINGS,
    VECTOR_SIZE,
    VOYAGE_MODEL,
)
from hackathon.core.embeddings import embed_query
from hackathon.core.repository import (
    database_ping,
    fetch_item,
    fetch_user_profile,
    get_dataset_stats,
    list_sample_items,
    list_sample_user_ids,
)
from hackathon.core.vector_store import vector_store
from hackathon.eval.metrics import load_eval_snapshot

logger = logging.getLogger("hackathon.system")

TAG_SYSTEM = "System"
TAG_DEMO = "Demo helpers"
TAG_AGENT = "Agent introspection"

_START_TIME = time.monotonic()
_BUILD_VERSION = os.getenv("HACKATHON_BUILD_VERSION", "1.0.0")
_IMAGE_NAME = os.getenv("HACKATHON_IMAGE_NAME", "entivia-hackathon")

system_router = APIRouter(tags=[TAG_SYSTEM])
demo_router = APIRouter(tags=[TAG_DEMO])
agent_router = APIRouter(tags=[TAG_AGENT])


# ── Cached dependency checks ──────────────────────────────────────────────────
# /healthz can be polled frequently (e.g. load-balancer probes). We cache deep
# checks so we never hit Postgres or Qdrant more than once per CHECK_TTL_SECONDS
# even under heavy probing.

_CHECK_TTL_SECONDS = 10.0


@dataclass
class _CacheSlot:
    value: object | None = None
    expires_at: float = 0.0


_cache: dict[str, _CacheSlot] = {
    "database": _CacheSlot(),
    "qdrant": _CacheSlot(),
}


async def _cached(key: str, fetch: Callable[[], Awaitable[object]]) -> object:
    slot = _cache[key]
    now = time.monotonic()
    if slot.value is not None and slot.expires_at > now:
        return slot.value
    value = await fetch()
    slot.value = value
    slot.expires_at = now + _CHECK_TTL_SECONDS
    return value


# ── Check builders ────────────────────────────────────────────────────────────


def _llm_check() -> LLMCheck:
    anthropic = bool((os.getenv("ANTHROPIC_API_KEY") or "").strip())
    groq = bool((os.getenv("GROQ_API_KEY") or "").strip())
    primary = HACKATHON_LLM_PROVIDER if HACKATHON_LLM_PROVIDER in {"anthropic", "groq"} else "anthropic"
    return LLMCheck(
        primary=primary,  # type: ignore[arg-type]
        anthropic_configured=anthropic,
        groq_configured=groq,
        fallback_ready=anthropic or groq,
    )


def _embeddings_check() -> EmbeddingsCheck:
    backend = "pseudo" if USE_PSEUDO_EMBEDDINGS else EMBEDDING_BACKEND
    if backend == "voyage":
        model = VOYAGE_MODEL
        configured = bool((os.getenv("VOYAGEAI_API_KEY") or "").strip())
    elif backend == "fastembed":
        model = FASTEMBED_MODEL
        configured = True  # local model, no credentials required
    else:
        model = "deterministic-hash"
        configured = True
    return EmbeddingsCheck(
        backend=backend,
        model=model,
        vector_size=VECTOR_SIZE,
        credentials_configured=configured,
        pseudo_mode=USE_PSEUDO_EMBEDDINGS or backend == "pseudo",
    )


async def _database_check() -> DatabaseCheck:
    configured = bool((HACKATHON_DATABASE_URL or "").strip())
    ok, latency, err = await database_ping()
    if not ok:
        return DatabaseCheck(configured=configured, ok=False, error=err)

    counts: dict[str, int | None] = {"users": None, "items": None, "reviews": None}
    try:
        stats = await get_dataset_stats()
        counts = {
            "users": stats["users"],
            "items": stats["items"],
            "reviews": stats["reviews"],
        }
    except Exception as exc:  # tables may not exist yet on first boot
        logger.debug("stats unavailable for /healthz: %s", exc)

    return DatabaseCheck(
        configured=configured,
        ok=True,
        latency_ms=latency,
        users=counts["users"],
        items=counts["items"],
        reviews=counts["reviews"],
    )


async def _qdrant_check() -> QdrantCheck:
    configured = bool((settings.QDRANT_URL or "").strip())
    ok, latency, err = await vector_store.ping()
    if not ok:
        return QdrantCheck(
            configured=configured,
            ok=False,
            collection=HACKATHON_QDRANT_COLLECTION,
            error=err,
        )
    info: dict[str, object] = {}
    try:
        info = await vector_store.info()
    except Exception as exc:
        logger.debug("collection info unavailable: %s", exc)
    return QdrantCheck(
        configured=configured,
        ok=True,
        latency_ms=latency,
        collection=str(info.get("collection", HACKATHON_QDRANT_COLLECTION)),
        points=info.get("points"),  # type: ignore[arg-type]
        vector_size=info.get("vector_size"),  # type: ignore[arg-type]
    )


def _task_id(request: Request) -> str:
    return getattr(request.app.state, "task_id", "combined")


# ── System ────────────────────────────────────────────────────────────────────


@system_router.get(
    "/healthz",
    response_model=HealthResponse,
    summary="Health check with dependency status",
    description=(
        "Always returns HTTP 200. The `status` field summarizes whether the "
        "service can fully serve traffic. Deep checks against Postgres and "
        "Qdrant are cached for 10 seconds so this stays cheap under heavy "
        "load-balancer polling. For a strict probe that returns 503 when a "
        "required dependency is down, see `GET /readyz`."
    ),
)
async def healthz(request: Request) -> HealthResponse:
    db = await _cached("database", _database_check)  # type: ignore[assignment]
    qd = await _cached("qdrant", _qdrant_check)  # type: ignore[assignment]
    assert isinstance(db, DatabaseCheck) and isinstance(qd, QdrantCheck)

    llm = _llm_check()
    embeddings = _embeddings_check()

    if not db.ok or not qd.ok:
        overall = "unhealthy"
    elif not llm.fallback_ready or not embeddings.credentials_configured:
        overall = "degraded"
    else:
        overall = "healthy"

    return HealthResponse(
        status=overall,
        task=_task_id(request),  # type: ignore[arg-type]
        uptime_seconds=round(time.monotonic() - _START_TIME, 2),
        checks=HealthChecks(database=db, qdrant=qd, llm=llm, embeddings=embeddings),
        version={"image": _IMAGE_NAME, "build": _BUILD_VERSION},
    )


@system_router.get(
    "/readyz",
    response_model=ReadinessResponse,
    summary="Strict readiness probe",
    description=(
        "Returns HTTP 200 only when Postgres, Qdrant, and at least one LLM "
        "provider are usable. Returns HTTP 503 with the failing dependencies "
        "listed otherwise. Suited for k8s readiness probes."
    ),
    responses={503: {"model": ReadinessResponse}},
)
async def readyz(response: Response) -> ReadinessResponse:
    db, qd = await _database_check(), await _qdrant_check()
    llm = _llm_check()
    failing: list[str] = []
    if not db.ok:
        failing.append("database")
    if not qd.ok:
        failing.append("qdrant")
    if not llm.fallback_ready:
        failing.append("llm")
    if failing:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(
            ready=False,
            failing=failing,
            detail=f"Required dependencies are not ready: {', '.join(failing)}",
        )
    return ReadinessResponse(ready=True)


@system_router.get(
    "/version",
    response_model=VersionResponse,
    summary="Build and configuration version",
    description="Image, build, embedding backend, and LLM provider for this container.",
)
async def version(request: Request) -> VersionResponse:
    embeddings = _embeddings_check()
    return VersionResponse(
        image=_IMAGE_NAME,
        build=_BUILD_VERSION,
        task=_task_id(request),  # type: ignore[arg-type]
        embedding_backend=embeddings.backend,
        embedding_model=embeddings.model,
        vector_size=embeddings.vector_size,
        primary_llm=_llm_check().primary,
        llm_model_override=HACKATHON_LLM_MODEL or None,
        qdrant_collection=HACKATHON_QDRANT_COLLECTION,
    )


# ── Demo ──────────────────────────────────────────────────────────────────────


@demo_router.get(
    "/stats",
    response_model=StatsResponse,
    summary="Overview of loaded data",
    description=(
        "Per-dataset counts of users, items, and reviews, plus the Qdrant "
        "collection size. Confirms the loader populated everything before "
        "judges start poking at `/recommend` or `/simulate-review`."
    ),
)
async def stats() -> StatsResponse:
    db_stats = await get_dataset_stats()
    points: int | None = None
    try:
        info = await vector_store.info()
        points = int(info.get("points") or 0)
    except Exception as exc:
        logger.debug("Qdrant unavailable for /stats: %s", exc)
    return StatsResponse(
        datasets=[DatasetStats(**row) for row in db_stats["datasets"]],
        users=db_stats["users"],
        items=db_stats["items"],
        reviews=db_stats["reviews"],
        holdout_reviews=db_stats["holdout_reviews"],
        qdrant_points=points,
    )


@demo_router.get(
    "/samples/users",
    response_model=SampleUsersResponse,
    summary="List sample warm-start user ids",
    description="Real ids from the loaded data — paste into `/simulate-review` or `/recommend`.",
)
async def sample_users(
    dataset: str = Query("yelp", description="Dataset to sample from, usually `yelp`."),
    limit: int = Query(5, ge=1, le=50),
) -> SampleUsersResponse:
    return SampleUsersResponse(
        dataset=dataset, user_ids=await list_sample_user_ids(dataset, limit)
    )


@demo_router.get(
    "/samples/items",
    response_model=SampleItemsResponse,
    summary="List sample items (id + name)",
    description=(
        "Real items from the loaded data. Use the returned `item_id` in DB-mode "
        "`/simulate-review` calls so you do not have to guess identifiers."
    ),
)
async def sample_items(
    dataset: str = Query("yelp"),
    limit: int = Query(5, ge=1, le=50),
) -> SampleItemsResponse:
    return SampleItemsResponse(
        dataset=dataset, items=await list_sample_items(dataset, limit)
    )


@demo_router.get(
    "/users/{user_id}",
    response_model=UserProfileResponse,
    summary="Inspect a user's persona profile",
    description=(
        "Returns the cached persona used by warm-start recommendation and review "
        "simulation: average rating, review count, top categories, and a few "
        "recent reviews. Useful for understanding why an agent produces what it does."
    ),
    responses={404: {"description": "User not found in the loaded data."}},
)
async def user_profile(user_id: str) -> UserProfileResponse:
    profile = await fetch_user_profile(user_id)
    if not profile:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"user {user_id} not found")
    profile.pop("user_vector", None)  # 1024-d vector adds noise to demos
    return UserProfileResponse(**profile)


@demo_router.get(
    "/items/{item_id}",
    response_model=ItemResponse,
    summary="Inspect a loaded item",
    description="Returns the item name and metadata exactly as the agents see it.",
    responses={404: {"description": "Item not found."}},
)
async def item_detail(item_id: str) -> ItemResponse:
    item = await fetch_item(item_id)
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"item {item_id} not found")
    return ItemResponse(**item)


@demo_router.get(
    "/search",
    response_model=SearchResponse,
    summary="Vector search over loaded items",
    description=(
        "Embeds the query with the configured embedding backend and runs an ANN "
        "lookup in Qdrant. Mirrors the candidate-retrieval stage of `/recommend` "
        "without the LLM rerank, so you can sanity-check embedding quality."
    ),
)
async def search(
    q: str = Query(..., min_length=1, description="Free-text query string."),
    dataset: str | None = Query(
        None, description="Restrict to a single dataset (e.g. `yelp`, `goodreads`)."
    ),
    k: int = Query(10, ge=1, le=50),
) -> SearchResponse:
    try:
        vector = embed_query(q)
        hits = await vector_store.search(vector, k=k, dataset=dataset)
    except Exception as exc:
        logger.exception("search failed")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    return SearchResponse(
        query=q,
        dataset=dataset,
        hits=[SearchHit(**hit) for hit in hits],
    )


@demo_router.get(
    "/metrics",
    response_model=MetricsResponse,
    summary="Latest offline evaluation report",
    description=(
        "Returns the most recent metrics generated by `python -m hackathon.eval.run`: "
        "Task A RMSE / ROUGE-L, Task B Hit@10 / NDCG@10, plus baseline ablations. "
        "If no eval has been run, `available=false` explains how to generate it."
    ),
)
async def metrics() -> MetricsResponse:
    return MetricsResponse(**load_eval_snapshot().to_dict())


# ── Standout demo endpoints ──────────────────────────────────────────────────


@demo_router.post(
    "/items/{item_id}/similar",
    response_model=ItemSimilarResponse,
    summary="Find items similar to a given item",
    description=(
        "Pure vector retrieval — embeds the seed item's name and metadata, then "
        "returns the nearest neighbours from Qdrant. Useful for an 'items like "
        "this one' UX without touching the LLM."
    ),
    responses={404: {"description": "Seed item not found."}},
)
async def items_similar(
    item_id: str,
    k: int = Query(10, ge=1, le=50),
) -> ItemSimilarResponse:
    try:
        item = await fetch_item(item_id)
    except Exception as exc:
        logger.warning("items/similar: DB unavailable: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is not reachable. See GET /healthz for status.",
        ) from exc
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"item {item_id} not found")

    parts = [item["name"]]
    metadata = item.get("metadata") or {}
    for key in ("categories", "genres", "description"):
        value = metadata.get(key)
        if value:
            parts.append(str(value))
    seed_text = " | ".join(parts)

    try:
        vector = embed_query(seed_text)
        raw_hits = await vector_store.search(
            vector, k=k + 1, dataset=item["dataset"], exclude_item_ids=[item_id]
        )
    except Exception as exc:
        logger.exception("items/similar failed")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Vector search failed: {exc}",
        ) from exc
    return ItemSimilarResponse(
        item_id=item_id,
        name=item["name"],
        dataset=item["dataset"],
        hits=[SearchHit(**hit) for hit in raw_hits[:k]],
    )


@demo_router.post(
    "/users/{user_id}/predict-rating",
    response_model=PredictRatingResponse,
    summary="Predict the star rating only (no review text)",
    description=(
        "Lightweight alternative to `/simulate-review` when you only need the "
        "predicted rating — for example, for batched Task A RMSE evaluation. "
        "Uses a smaller rating-only prompt/token budget and, in DB mode, "
        "prefetches the profile/item server-side to avoid extra tool hops."
    ),
)
async def predict_rating(
    user_id: str,
    payload: PredictRatingRequest = Body(default_factory=PredictRatingRequest),
) -> PredictRatingResponse:
    from hackathon.agents.review_simulator import ReviewSimulationAgent

    agent = ReviewSimulationAgent()
    try:
        if payload.persona and payload.product:
            result = await agent.predict_rating(
                user_id=user_id,
                item_id=payload.item_id,
                persona=payload.persona.to_agent_dict(),
                product=payload.product.to_agent_dict(),
            )
        elif payload.item_id:
            profile, item = await asyncio.gather(
                fetch_user_profile(user_id),
                fetch_item(payload.item_id),
            )
            if not profile:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    detail=f"user {user_id} not found",
                )
            if not item:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    detail=f"item {payload.item_id} not found",
                )
            result = await agent.predict_rating(
                user_id=user_id,
                item_id=payload.item_id,
                persona=profile,
                product=item,
                input_mode="db_prefetched",
            )
        else:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Provide (persona + product) or item_id for DB mode.",
            )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("predict-rating failed")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Rating prediction failed: {exc}",
        ) from exc

    meta = result.get("meta") or {}
    fallbacks = int(meta.get("provider_fallbacks", 0))
    retries = int(meta.get("validation_retries", 0))
    confidence = max(0.0, min(1.0, 1.0 - 0.25 * fallbacks - 0.1 * retries))

    return PredictRatingResponse(
        user_id=user_id,
        item_id=payload.item_id,
        predicted_stars=float(result["stars"]),
        confidence=round(confidence, 3),
        meta=meta,
    )


@demo_router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationResponse,
    summary="Inspect a multi-turn recommend conversation",
    description=(
        "Returns the conversation state used by `/recommend` for follow-up "
        "refinement: how many turns have been served, which item ids have "
        "already been shown, and which dataset they came from. The store is "
        "in-process and resets when the container restarts."
    ),
    responses={404: {"description": "Unknown conversation id."}},
)
async def conversation(conversation_id: str) -> ConversationResponse:
    from app.agents.workflows.cold_start_recommender import get_conversation_store

    session = get_conversation_store().get(conversation_id)
    if session is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"conversation {conversation_id} not found",
        )
    return ConversationResponse(
        conversation_id=conversation_id,
        dataset=session.get("dataset"),
        shown_ids=session.get("shown_ids", []),
        turns=session.get("turns", 0),
    )


# ── Agent introspection ──────────────────────────────────────────────────────


def _describe_tool(tool: object) -> AgentToolDescriptor:
    params = [
        AgentToolParamDescriptor(
            name=p.name,
            type=p.param_type,
            description=p.description,
            required=p.required,
            enum=list(p.enum) if p.enum else None,
        )
        for p in getattr(tool, "parameters", [])
    ]
    return AgentToolDescriptor(
        name=getattr(tool, "name", "?"),
        description=getattr(tool, "description", ""),
        parameters=params,
    )


@agent_router.get(
    "/agent/tools",
    response_model=AgentToolsResponse,
    summary="Describe the tools available to each agent",
    description=(
        "Returns the tool schemas registered on the Review Simulation Agent and "
        "the Recommendation Agent — the actual functions the LLM can call inside "
        "the ReAct loop. This is the difference between an LLM-with-a-prompt and "
        "an agent."
    ),
)
async def agent_tools() -> AgentToolsResponse:
    from hackathon.agents.recommender import RecommendationAgent
    from hackathon.agents.review_simulator import ReviewSimulationAgent

    rev = ReviewSimulationAgent()
    rec = RecommendationAgent()
    return AgentToolsResponse(
        agents=[
            AgentDescriptor(
                agent="review_simulator",
                provider=rev.provider.value,  # type: ignore[arg-type]
                fallback_enabled=getattr(rev, "fallback_enabled", True),
                tools=[_describe_tool(t) for t in rev.registry.tools],
            ),
            AgentDescriptor(
                agent="recommender",
                provider=rec.provider.value,  # type: ignore[arg-type]
                fallback_enabled=getattr(rec, "fallback_enabled", True),
                tools=[_describe_tool(t) for t in rec.registry.tools],
            ),
        ]
    )


async def _compare_run(
    provider: str,
    factory: Callable[[], Awaitable[dict]],
) -> ProviderRun:
    started = time.perf_counter()
    try:
        response = await factory()
    except Exception as exc:
        return ProviderRun(
            provider=provider,  # type: ignore[arg-type]
            ok=False,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error=str(exc),
        )
    return ProviderRun(
        provider=provider,  # type: ignore[arg-type]
        ok=True,
        duration_ms=int((time.perf_counter() - started) * 1000),
        response=response,
    )


@agent_router.post(
    "/compare",
    response_model=CompareResponse,
    summary="Run the same input through Anthropic and Groq in parallel",
    description=(
        "Side-by-side execution against both configured LLM providers. Lets you "
        "see the dual-provider story in action: latency, output differences, and "
        "which provider failed when one returns an error. Requires both "
        "`ANTHROPIC_API_KEY` and `GROQ_API_KEY` to be set."
    ),
)
async def compare(req: CompareRequest) -> CompareResponse:
    from app.agents.base import LLMProvider
    from hackathon.agents.recommender import RecommendationAgent
    from hackathon.agents.review_simulator import ReviewSimulationAgent

    if req.kind == "simulate-review":
        if not req.simulate_review:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Provide a `simulate_review` payload for kind='simulate-review'.",
            )
        sr = req.simulate_review

        async def _run(provider: LLMProvider) -> dict:
            agent = ReviewSimulationAgent()
            agent.provider = provider
            agent.default_model = (
                settings.ANTHROPIC_LLM_MODEL
                if provider == LLMProvider.ANTHROPIC
                else settings.GROQ_LLM_MODEL
            )
            agent.fallback_enabled = False
            if sr.persona and sr.product:
                return await agent.simulate(
                    persona=sr.persona.to_agent_dict(),
                    product=sr.product.to_agent_dict(),
                    voice=sr.voice,
                )
            return await agent.simulate(
                user_id=sr.user_id, item_id=sr.item_id, voice=sr.voice
            )

        runs = await asyncio.gather(
            _compare_run("anthropic", lambda: _run(LLMProvider.ANTHROPIC)),
            _compare_run("groq", lambda: _run(LLMProvider.GROQ)),
        )
    else:
        if not req.recommend:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Provide a `recommend` payload for kind='recommend'.",
            )
        rec = req.recommend

        async def _run(provider: LLMProvider) -> dict:
            agent = RecommendationAgent()
            agent.provider = provider
            agent.default_model = (
                settings.ANTHROPIC_LLM_MODEL
                if provider == LLMProvider.ANTHROPIC
                else settings.GROQ_LLM_MODEL
            )
            agent.fallback_enabled = False
            return await agent.recommend(
                user_id=rec.user_id,
                persona=rec.persona,
                k=rec.k,
                dataset=rec.dataset,
            )

        runs = await asyncio.gather(
            _compare_run("anthropic", lambda: _run(LLMProvider.ANTHROPIC)),
            _compare_run("groq", lambda: _run(LLMProvider.GROQ)),
        )

    return CompareResponse(kind=req.kind, runs=list(runs))
