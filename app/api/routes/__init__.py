from app.api.routes.agent import router as agent_router
from app.api.routes.alerts import router as alerts_router
from app.api.routes.connections import router as connections_router
from app.api.routes.dashboard import router as dashboard_router
from app.api.routes.entities import router as entities_router
from app.api.routes.onboarding import router as onboarding_router
from app.api.routes.organization import router as org_router
from app.api.routes.recommendations import router as recommendations_router
from app.api.routes.schema_mappings import router as schema_mappings_router
from app.api.routes.users import router as users_router

__all__ = [
    "agent_router",
    "alerts_router",
    "connections_router",
    "dashboard_router",
    "entities_router",
    "onboarding_router",
    "org_router",
    "recommendations_router",
    "schema_mappings_router",
    "users_router",
]
