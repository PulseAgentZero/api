"""Shared FastAPI factory helpers for per-task hackathon apps."""

from __future__ import annotations

import logging
import os
from typing import Literal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from hackathon.app.system_router import (
    TAG_AGENT,
    TAG_DEMO,
    TAG_SYSTEM,
    agent_router,
    demo_router,
    system_router,
)

logger = logging.getLogger("hackathon.api")

TaskId = Literal["task_a", "task_b", "combined"]


def openapi_servers() -> list[dict[str, str]] | None:
    """Expose the public API origin in Swagger when deployed behind nginx."""
    public_url = os.getenv("HACKATHON_PUBLIC_API_URL", "").strip().rstrip("/")
    if not public_url:
        return None
    return [{"url": public_url, "description": "Production API endpoint"}]


def create_app(
    *,
    title: str,
    description: str,
    task_id: TaskId,
    version: str = "1.0.0",
    extra_tags: list[dict[str, str]] | None = None,
) -> FastAPI:
    """Build a hackathon FastAPI app with system + demo routers pre-mounted."""

    tags: list[dict[str, str]] = [
        {"name": TAG_SYSTEM, "description": "Health, readiness, and build metadata."},
        {"name": TAG_DEMO, "description": "Helpers for exploring loaded data and the retrieval layer."},
        {"name": TAG_AGENT, "description": "Introspect tools and compare LLM providers side-by-side."},
    ]
    if extra_tags:
        tags.extend(extra_tags)

    app = FastAPI(
        title=title,
        description=description,
        version=version,
        contact={"name": "Entivia / Pulse AI"},
        license_info={"name": "Hackathon submission"},
        servers=openapi_servers(),
        openapi_tags=tags,
    )
    app.state.task_id = task_id
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(system_router)
    app.include_router(demo_router)
    app.include_router(agent_router)
    return app


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")
