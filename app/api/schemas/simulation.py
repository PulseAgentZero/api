"""Pydantic schemas shared by the hackathon containers and the production
``/api/public/v1/simulation/*`` routes.

These are the *agent-level* contracts (persona, product, review, recommendation,
runtime meta). The hackathon FastAPI surface adds a few demo-only models on top
(``HealthResponse``, ``SampleUsersResponse``, ``MetricsResponse``, eval records),
which continue to live in :mod:`hackathon.app.schemas`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SAMPLE_USER_ID = "_BcWyKQL16ndpBdggh2kNA"
_SAMPLE_ITEM_ID = "EoQiJ5D-pyWczjElN24oZg"


class AgentRunMeta(BaseModel):
    """Per-request agent observability counters."""

    agent: str = Field(..., description="Agent name from BaseAgent.")
    model: str = Field(..., description="Primary model configured for this run.")
    primary_provider: str = Field(..., description="Requested primary LLM provider.")
    providers_used: list[str] = Field(default_factory=list, description="LLM providers actually used.")
    llm_calls: int = Field(0, description="Number of LLM calls made by the ReAct loop.")
    tool_calls: int = Field(0, description="Number of registered tools invoked.")
    tool_failures: int = Field(0, description="Tool calls that returned failures.")
    prompt_tokens: int = Field(0, description="Prompt/input tokens reported by the provider.")
    completion_tokens: int = Field(0, description="Completion/output tokens reported by the provider.")
    total_tokens: int = Field(0, description="Prompt + completion tokens.")
    llm_duration_ms: int = Field(0, description="Provider-reported/loop-measured LLM time.")
    latency_ms: int = Field(0, description="Wall-clock endpoint agent time.")
    validation_retries: int = Field(0, description="JSON validation retries in BaseAgent.")
    provider_fallbacks: int = Field(0, description="Fallbacks from primary provider to backup provider.")
    task: str = Field(..., description="Task label, e.g. review_simulation or recommendation.")
    input_mode: str | None = Field(None, description="Task A: `direct` or `db`.")
    voice: str | None = Field(None, description="Review voice for Task A.")
    mode: str | None = Field(None, description="Recommendation mode for Task B.")
    embedding_backend: str | None = Field(None, description="Embedding backend used before rerank.")
    candidate_pool_size: int | None = Field(None, description="ANN candidate count offered to the agent.")
    excluded_items_count: int | None = Field(None, description="Already-seen item ids excluded from retrieval.")
    top_ann_score: float | None = Field(None, description="Highest ANN similarity score in the candidate pool.")


class PersonaInput(BaseModel):
    """User persona for direct-input review simulation."""

    description: str = Field(
        ...,
        description="Free-text persona, e.g. rating style and food preferences.",
        examples=[
            "Generous reviewer who loves spicy Nigerian food, casual dining, and cafes. "
            "Writes short enthusiastic reviews."
        ],
    )
    avg_stars: float | None = Field(None, ge=1, le=5, description="Typical star rating tendency.")
    top_categories: list[str] = Field(
        default_factory=list,
        description="Preferred categories, e.g. ['Restaurants', 'Nigerian', 'Bars'].",
    )
    sample_reviews: list[str] = Field(
        default_factory=list,
        description="Optional snippets of past reviews to mimic writing style.",
    )

    def to_agent_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class ProductInput(BaseModel):
    """Product / business details for direct-input review simulation."""

    name: str = Field(..., description="Business or product name.", examples=["Tam Tam African Restaurant"])
    categories: str | None = Field(None, description="Category tags.", examples=["African, Restaurants"])
    city: str | None = Field(None, examples=["Philadelphia"])
    state: str | None = Field(None, examples=["PA"])
    stars: float | None = Field(None, ge=1, le=5, description="Aggregate business rating if known.")
    description: str | None = Field(None, description="Extra product context.")

    def to_agent_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class SimulateReviewRequest(BaseModel):
    """Predict how a persona would review a given product.

    **Direct mode:** ``persona`` + ``product`` (no database).
    **DB mode (hackathon containers only):** ``user_id`` + ``item_id``.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "persona": {
                        "description": (
                            "Generous reviewer who loves spicy Nigerian food and casual dining. "
                            "Usually rates 4-5 stars."
                        ),
                        "avg_stars": 4.2,
                        "top_categories": ["Nigerian", "Restaurants", "Bars"],
                        "sample_reviews": [
                            "The jollof was fire - portions generous and service quick."
                        ],
                    },
                    "product": {
                        "name": "Tam Tam African Restaurant",
                        "categories": "African, Restaurants",
                        "city": "Philadelphia",
                        "stars": 3.5,
                    },
                    "voice": "default",
                },
                {"user_id": _SAMPLE_USER_ID, "item_id": _SAMPLE_ITEM_ID, "voice": "default"},
                {"user_id": _SAMPLE_USER_ID, "item_id": _SAMPLE_ITEM_ID, "voice": "nigerian"},
            ]
        }
    )

    user_id: str | None = Field(
        None,
        description="DB mode: real Yelp user id. Use GET /samples/users for examples.",
        examples=[_SAMPLE_USER_ID],
    )
    item_id: str | None = Field(
        None,
        description="DB mode: real Yelp business id.",
        examples=[_SAMPLE_ITEM_ID],
    )
    persona: PersonaInput | None = Field(
        None,
        description="Direct mode: user persona object (required with `product`).",
    )
    product: ProductInput | None = Field(
        None,
        description="Direct mode: product/business details (required with `persona`).",
    )
    voice: Literal["default", "nigerian"] = Field(
        "default",
        description=(
            "Prompt style. `nigerian` keeps the prediction grounded but uses a "
            "Nigerian expressive voice."
        ),
    )

    @model_validator(mode="after")
    def check_input_mode(self) -> "SimulateReviewRequest":
        direct = self.persona is not None and self.product is not None
        db = bool(self.user_id and self.item_id)
        if direct and db:
            raise ValueError("Use either (persona + product) OR (user_id + item_id), not both.")
        if not direct and not db:
            raise ValueError("Provide (persona + product) or (user_id + item_id).")
        if self.persona is not None and self.product is None:
            raise ValueError("`product` is required when `persona` is provided.")
        if self.product is not None and self.persona is None:
            raise ValueError("`persona` is required when `product` is provided.")
        return self


class SimulateReviewResponse(BaseModel):
    """Task A output: predicted star rating plus generated review text."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "user_id": _SAMPLE_USER_ID,
                    "item_id": _SAMPLE_ITEM_ID,
                    "stars": 4,
                    "text": (
                        "Still one of the best-kept secrets in Norristown. The al "
                        "pastor was generous, flavorful, and great value. Service "
                        "was warm, and I would come back."
                    ),
                    "meta": {
                        "agent": "review_simulator",
                        "model": "claude-sonnet-4-6",
                        "primary_provider": "anthropic",
                        "providers_used": ["anthropic"],
                        "llm_calls": 2,
                        "tool_calls": 2,
                        "tool_failures": 0,
                        "prompt_tokens": 1320,
                        "completion_tokens": 210,
                        "total_tokens": 1530,
                        "llm_duration_ms": 4100,
                        "latency_ms": 4500,
                        "validation_retries": 0,
                        "provider_fallbacks": 0,
                        "task": "review_simulation",
                        "voice": "default",
                    },
                }
            ]
        }
    )

    user_id: str | None = Field(None, description="User id when DB mode was used.")
    item_id: str | None = Field(None, description="Item id when DB mode was used.")
    stars: int = Field(..., ge=1, le=5, description="Predicted Yelp-style star rating.")
    text: str = Field(..., description="Generated review in the selected voice.")
    meta: AgentRunMeta = Field(..., description="Observed runtime counters for this agent call.")


class RecommendRequest(BaseModel):
    """Task B input: recommend items for warm-start or cold-start users."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"user_id": _SAMPLE_USER_ID, "k": 5, "dataset": "yelp"},
                {
                    "persona": "loves spicy Nigerian jollof, suya, and late-night food spots",
                    "k": 5,
                    "dataset": "yelp",
                },
                {
                    "persona": "African literary fiction and historical novels",
                    "k": 5,
                    "dataset": "goodreads",
                },
                {
                    "conversation_id": "paste-a-previous-conversation-id",
                    "follow_up": "Make the next set more casual and good for a group dinner",
                    "persona": "loves spicy Nigerian food",
                    "k": 5,
                    "dataset": "yelp",
                },
            ]
        }
    )

    user_id: str | None = Field(
        None,
        description=(
            "Warm-start user id. When provided, the agent builds recommendations "
            "from the user's real review history."
        ),
        examples=[_SAMPLE_USER_ID],
    )
    persona: str | None = Field(
        None,
        description="Cold-start persona text. Use this when there is no known user history.",
        examples=["loves spicy Nigerian jollof, suya, and late-night food spots"],
    )
    k: int = Field(10, ge=1, le=50, description="Number of recommendations to return.")
    dataset: Literal["yelp", "goodreads"] = Field(
        "yelp",
        description=(
            "`yelp` returns restaurants/businesses; `goodreads` demonstrates "
            "cross-domain book recommendations."
        ),
    )
    conversation_id: str | None = Field(
        None,
        description=(
            "Conversation id returned by a previous /recommend call. Used for "
            "multi-turn refinement and avoiding repeated items."
        ),
    )
    follow_up: str | None = Field(
        None,
        description="Natural-language refinement, e.g. 'make it cheaper' or 'avoid bars'.",
        examples=["Make the next set more casual and good for a group dinner"],
    )


class RecommendItem(BaseModel):
    item_id: str = Field(..., description="Recommended item id.")
    name: str = Field("", description="Display name from Yelp/Goodreads metadata.")
    why: str = Field("", description="Short agent rationale for why this item fits.")


class RecommendResponse(BaseModel):
    """Task B output: ranked recommendations plus an agent rationale."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "items": [
                        {
                            "item_id": "d9vapPUlJCSSrltRRKO9ig",
                            "name": "Sushi Avenue",
                            "why": (
                                "Matches the user's strong history of highly "
                                "rated Asian restaurants and casual dining."
                            ),
                        }
                    ],
                    "rationale": (
                        "The list balances ANN retrieval with LLM reranking over "
                        "the user's real review history."
                    ),
                    "conversation_id": "d42e75ba-c865-4c00-a1a7-b45308219a33",
                    "dataset": "yelp",
                    "meta": {
                        "agent": "recommender",
                        "model": "claude-sonnet-4-6",
                        "primary_provider": "anthropic",
                        "providers_used": ["anthropic"],
                        "llm_calls": 2,
                        "tool_calls": 1,
                        "tool_failures": 0,
                        "prompt_tokens": 2600,
                        "completion_tokens": 360,
                        "total_tokens": 2960,
                        "llm_duration_ms": 5200,
                        "latency_ms": 5900,
                        "validation_retries": 0,
                        "provider_fallbacks": 0,
                        "task": "recommendation",
                        "mode": "warm-start",
                        "embedding_backend": "fastembed",
                        "candidate_pool_size": 30,
                        "excluded_items_count": 23,
                        "top_ann_score": 0.86,
                    },
                }
            ]
        }
    )

    items: list[RecommendItem] = Field(..., description="Ranked recommendation list.")
    rationale: str = Field(..., description="Overall explanation of the recommendation strategy.")
    conversation_id: str = Field(..., description="Use this id for follow-up /recommend refinements.")
    dataset: str = Field(..., description="Dataset used for the response.")
    meta: AgentRunMeta = Field(..., description="Observed runtime counters and retrieval metadata.")
