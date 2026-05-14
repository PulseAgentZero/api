"""
Standalone conversational AI ASGI app (same routes as ``/api/v1/agent`` on main API).

Run::

    uvicorn app.conversational.app:app --host 0.0.0.0 --port 8001

- Auth: ``Authorization: Bearer <jwt>`` (same as internal API)
- Paths mirror main API: ``/api/v1/agent/conversations``, ``/api/v1/agent/chat``, etc.
- Business logic: ``app.services.agent_service``
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.exception_handlers import attach_exception_handlers
from app.api.middleware import LoggingMiddleware
from app.config.settings import settings
from app.conversational.router import router as agent_chat_router
from app.infrastructure.database.session import async_session_factory
from app.infrastructure.logging import configure_logging

configure_logging()

_is_prod = settings.is_production()

_openapi_tags = [
    {
        "name": "Agent",
        "description": "Dashboard conversational AI — list conversations, chat, same JWT as main API",
    },
    {"name": "System", "description": "Service health"},
]


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """No pipeline schedulers — this process is stateless aside from the DB pool."""
    yield


app = FastAPI(
    title="Pulse — Conversational service",
    description=(
        "Dedicated process for dashboard chat.\n\n"
        "**Auth:** `Authorization: Bearer <jwt_token>` (same tokens as the main internal API)\n\n"
        "**Paths:** Same as main API under `/api/v1/agent/...` (e.g. point a reverse proxy here on port 8001)."
    ),
    version="1.0.0",
    openapi_tags=_openapi_tags,
    lifespan=_lifespan,
    docs_url=None if _is_prod else "/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(LoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

attach_exception_handlers(app)

app.include_router(agent_chat_router, prefix="/api/v1")


@app.get("/health", tags=["System"])
async def health_check() -> dict:
    db_status = "healthy"
    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        db_status = "unhealthy"
    return {
        "status": "healthy" if db_status == "healthy" else "unhealthy",
        "database": db_status,
        "service": "pulse-conversational",
    }
