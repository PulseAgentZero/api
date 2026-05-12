from pydantic import BaseModel, Field

from app.api.schemas.connection import ConnectionResponse
from app.api.schemas.schema_mapping import SchemaMappingResponse


class OnboardingContextRequest(BaseModel):
    industry: str | None = Field(None, max_length=100)
    business_context: str = Field(..., min_length=1)
    entity_label: str = Field(..., min_length=1, max_length=100)
    goal_label: str = Field(..., min_length=1, max_length=255)


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
