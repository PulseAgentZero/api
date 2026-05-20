from app.api.public.analytics import router as public_analytics_router
from app.api.public.entities import router as public_entities_router
from app.api.public.pipeline import router as public_pipeline_router
from app.api.public.recommendations import router as public_recommendations_router
from app.api.public.studio_dashboard import router as public_studio_router

__all__ = [
    "public_analytics_router",
    "public_entities_router",
    "public_pipeline_router",
    "public_recommendations_router",
    "public_studio_router",
]
