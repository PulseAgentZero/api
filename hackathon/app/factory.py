"""Shared FastAPI factory helpers for per-task hackathon apps."""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger("hackathon.api")


def openapi_servers() -> list[dict[str, str]] | None:
    """Expose the public API origin in Swagger when deployed behind nginx."""
    public_url = os.getenv("HACKATHON_PUBLIC_API_URL", "").strip().rstrip("/")
    if not public_url:
        return None
    return [{"url": public_url, "description": "Production API endpoint"}]


def create_app(*, title: str, description: str, version: str = "1.0.0") -> FastAPI:
    app = FastAPI(
        title=title,
        description=description,
        version=version,
        contact={"name": "Entivia / Pulse AI"},
        license_info={"name": "Hackathon submission"},
        servers=openapi_servers(),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return app


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")
