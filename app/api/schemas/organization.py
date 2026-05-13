from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class UpdateOrgRequest(BaseModel):
    name: str | None = None
    industry: str | None = None
    business_context: str | None = None
    entity_label: str | None = None
    goal_label: str | None = None
    timezone: str | None = None
    logo_url: str | None = None


class OrgProfileResponse(BaseModel):
    id: UUID
    name: str
    slug: str | None
    industry: str | None
    business_context: str | None
    entity_label: str | None
    goal_label: str | None
    plan: str | None
    timezone: str | None
    logo_url: str | None
    onboarding_done: bool
    created_at: datetime
    updated_at: datetime
