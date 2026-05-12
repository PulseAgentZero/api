from uuid import UUID

from pydantic import BaseModel


class UpdateOrgRequest(BaseModel):
    industry: str | None = None
    business_context: str | None = None
    entity_label: str | None = None
    goal_label: str | None = None


class OrgProfileResponse(BaseModel):
    id: UUID
    name: str
    industry: str | None
    business_context: str | None
    entity_label: str | None
    goal_label: str | None
    onboarding_done: bool
