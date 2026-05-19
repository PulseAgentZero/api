from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.auth import auth_router
from app.api.exception_handlers import attach_exception_handlers
from app.api.middleware import LoggingMiddleware
from app.api.routes import (
    agent_router,
    alerts_router,
    analytics_router,
    api_keys_router,
    audit_logs_router,
    billing_router,
    connections_router,
    dashboard_router,
    entities_router,
    license_router,
    notifications_router,
    org_router,
    pipeline_router,
    recommendations_router,
    schema_mappings_router,
    settings_router,
    studio_router,
    users_router,
    webhooks_router,
)
from app.api.public import (
    public_analytics_router,
    public_entities_router,
    public_pipeline_router,
    public_recommendations_router,
    public_studio_router,
)
from app.api.public.openapi import configure_public_openapi
from app.config.settings import settings
from app.infrastructure.database.session import async_session_factory
from app.infrastructure.logging import configure_logging
from app.infrastructure.redis.client import close_redis

configure_logging()

# Mount local file storage if LOCAL backend is active
def _mount_local_storage(app: "FastAPI") -> None:
    try:
        from app.infrastructure.storage.factory import get_storage_backend
        from app.infrastructure.storage.local import LocalBackend
        backend = get_storage_backend()
        if not isinstance(backend, LocalBackend):
            return
        from fastapi.staticfiles import StaticFiles
        path = backend.storage_path
        try:
            path.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            import logging as _log
            _log.getLogger(__name__).warning(
                "Local storage: cannot create %s (permission denied). "
                "Files will still upload but /assets won't be served. "
                "Set LOCAL_STORAGE_PATH to a writable directory or use STORAGE_BACKEND=s3/minio.",
                path,
            )
            return
        app.mount("/assets", StaticFiles(directory=str(path)), name="assets")
    except Exception as exc:
        import logging as _log
        _log.getLogger(__name__).warning("Could not mount local storage: %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    # All APScheduler crons run in the dedicated scheduler process only
    # (docker compose `scheduler`, self-hosted supervisord, or `python -m app.services.schedulers.run`).
    try:
        yield
    finally:
        await close_redis()


# ── Internal API ──────────────────────────────────────────────────────────────
# Used by the Pulse dashboard (frontend).
# Auth: JWT Bearer token only.

_is_prod = settings.is_production()

_internal_tags = [
    {"name": "Auth",            "description": "Signup, login, token refresh, OAuth, email verification, password reset"},
    {"name": "Organization",    "description": "Org profile management"},
    {"name": "Users",           "description": "User management, invitations, roles"},
    {"name": "Connections",     "description": "Data source connectors — databases, spreadsheets, cloud warehouses, file uploads"},
    {"name": "Schema Mappings", "description": "Map entity tables, signal columns, and risk config"},
    {"name": "Pipeline",        "description": "Trigger and monitor AI intelligence pipeline runs"},
    {"name": "Agent",           "description": "Dashboard conversational AI over org intelligence"},
    {"name": "Dashboard",       "description": "High-level org overview and KPIs"},
    {"name": "Entities",        "description": "Browse and inspect profiled entities"},
    {"name": "Recommendations", "description": "AI-generated recommendations and actions"},
    {"name": "Analytics",       "description": "Trends, cohorts, segments, and exports"},
    {"name": "Alerts",          "description": "Alert rules and notification channels"},
    {"name": "Notifications",   "description": "In-app notification inbox"},
    {"name": "Webhooks",        "description": "Outbound webhook delivery log"},
    {"name": "API Keys",        "description": "Programmatic API access"},
    {"name": "License",         "description": "Self-hosted license activation (self-hosted only)"},
    {"name": "Settings",        "description": "LLM key management (self-hosted only)"},
    {"name": "Audit Logs",      "description": "Immutable audit trail (Pro only)"},
    {"name": "Billing",         "description": "Paystack subscription management and webhooks"},
    {"name": "Studio",          "description": "SQL query editor, visualizations, and custom dashboards"},
    {"name": "System",          "description": "Service health and readiness"},
]

app = FastAPI(
    title="Pulse — Internal API",
    description=(
        "Internal API used by the Pulse dashboard.\n\n"
        "**Auth:** `Authorization: Bearer <jwt_token>`\n\n"
        "**Errors:** `{ \"error\": { \"code\": \"string\", \"message\": \"string\" } }`"
    ),
    version="1.0.0",
    contact={"name": "Pulse Support", "email": "support@pulseai.io"},
    license_info={"name": "Proprietary"},
    openapi_tags=_internal_tags,
    lifespan=lifespan,
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

app.include_router(auth_router,            prefix="/api/v1")
app.include_router(org_router,             prefix="/api/v1")
app.include_router(connections_router,     prefix="/api/v1")
app.include_router(schema_mappings_router, prefix="/api/v1")
app.include_router(dashboard_router,       prefix="/api/v1")
app.include_router(entities_router,        prefix="/api/v1")
app.include_router(recommendations_router, prefix="/api/v1")
app.include_router(alerts_router,          prefix="/api/v1")
app.include_router(analytics_router,       prefix="/api/v1")
app.include_router(notifications_router,   prefix="/api/v1")
app.include_router(webhooks_router,        prefix="/api/v1")
app.include_router(api_keys_router,        prefix="/api/v1")
app.include_router(license_router,         prefix="/api/v1")
app.include_router(settings_router,        prefix="/api/v1")
app.include_router(audit_logs_router,      prefix="/api/v1")
app.include_router(agent_router,           prefix="/api/v1")
app.include_router(users_router,           prefix="/api/v1")
app.include_router(pipeline_router,        prefix="/api/v1")
app.include_router(billing_router,         prefix="/api/v1")
app.include_router(studio_router,          prefix="/api/v1")


# ── Public API ────────────────────────────────────────────────────────────────
# Used by external developers and integrations.
# Auth: X-API-Key header only. JWT tokens are rejected.
# Mounted as a sub-application so it has its own /docs, /redoc, /openapi.json.

_public_tags = [
    {"name": "Entities"},
    {"name": "Recommendations"},
    {"name": "Pipeline"},
    {"name": "Analytics"},
    {"name": "Studio"},
]

public_app = FastAPI(
    title="Pulse Public API",
    description="See the overview below for authentication, envelopes, and rate limits.",
    version="1.0.0",
    contact={"name": "Pulse Support", "email": "support@pulseai.io"},
    openapi_tags=_public_tags,
    docs_url=None if _is_prod else "/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

configure_public_openapi(public_app)
attach_exception_handlers(public_app)

public_app.include_router(public_entities_router,        prefix="/v1")
public_app.include_router(public_recommendations_router, prefix="/v1")
public_app.include_router(public_pipeline_router,        prefix="/v1")
public_app.include_router(public_analytics_router,       prefix="/v1")
public_app.include_router(public_studio_router,          prefix="/v1")

# Mount public_app under /api/public
# Routes become: /api/public/v1/entities, /api/public/docs, etc.
app.mount("/api/public", public_app)
_mount_local_storage(app)


# ── Health check ──────────────────────────────────────────────────────────────

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
        "service": "pulse",
    }
