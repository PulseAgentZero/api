"""Shared FastAPI factory helpers for per-task hackathon apps."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger("hackathon.api")


def create_app(*, title: str, description: str, version: str = "1.0.0") -> FastAPI:
    app = FastAPI(
        title=title,
        description=description,
        version=version,
        contact={"name": "Entivia / Pulse AI"},
        license_info={"name": "Hackathon submission"},
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
