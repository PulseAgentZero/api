from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.auth import auth_router
from app.api.routes import (
    connections_router,
    onboarding_router,
    org_router,
    schema_mappings_router,
)
from app.config.settings import settings
from app.infrastructure.database.session import async_session_factory

logger = __import__("logging").getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    yield


_is_prod = settings.is_production()
app = FastAPI(
    title="Pulse API",
    description="Real-Time Intelligence for Any Business",
    version="2.0.0",
    lifespan=lifespan,
    docs_url=None if _is_prod else "/docs",
    redoc_url=None if _is_prod else "/redoc",
    openapi_url=None if _is_prod else "/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "Unhandled exception on %s %s: %s: %s",
        request.method,
        request.url.path,
        type(exc).__name__,
        exc,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


app.include_router(auth_router, prefix="/api/v1")
app.include_router(org_router, prefix="/api/v1")
app.include_router(connections_router, prefix="/api/v1")
app.include_router(schema_mappings_router, prefix="/api/v1")
app.include_router(onboarding_router, prefix="/api/v1")


@app.get("/health")
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
        "service": "pulse",
    }
