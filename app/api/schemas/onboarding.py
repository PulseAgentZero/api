from pydantic import BaseModel, Field

from app.api.schemas.connection import ConnectionResponse
from app.api.schemas.schema_mapping import SchemaMappingResponse


class OnboardingContextRequest(BaseModel):
    """All fields optional — callers may send only what they have; skip this step entirely if preferred."""

    industry: str | None = Field(None, max_length=100)
    business_context: str | None = Field(None, max_length=20_000)
    entity_label: str | None = Field(None, max_length=100)
    goal_label: str | None = Field(None, max_length=255)


class OnboardingConnectionResponse(BaseModel):
    connection: ConnectionResponse
    success: bool
    message: str
    db_version: str | None = None


class OnboardingSchemaMappingResponse(BaseModel):
    schema_mapping: SchemaMappingResponse


class CompleteOnboardingResponse(BaseModel):
    message: str
    onboarding_done: bool
    generated_recommendations: int = 0
