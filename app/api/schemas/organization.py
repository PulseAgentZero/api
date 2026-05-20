from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class MemberSettingsRequest(BaseModel):
    """Product tour state for any member; org context fields require manager/admin."""

    industry: str | None = Field(None, max_length=100)
    business_context: str | None = Field(None, max_length=20_000)
    entity_label: str | None = Field(None, max_length=100)
    goal_label: str | None = Field(None, max_length=255)
    tour_guide: dict[str, Any] | None = None


class CompleteSetupResponse(BaseModel):
    message: str
    onboarding_done: bool
    generated_recommendations: int = 0


class UpdateOrgRequest(BaseModel):
    name: str | None = None
    industry: str | None = None
    business_context: str | None = None
    entity_label: str | None = None
    goal_label: str | None = None
    timezone: str | None = None
    logo_url: str | None = None
    tour_guide: dict[str, Any] | None = None


class OrgSecurityRequest(BaseModel):
    require_2fa: bool


class OrgSecurityResponse(BaseModel):
    require_2fa: bool


class DeleteOrgConfirmRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6)


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
    tour_guide: dict[str, Any] = Field(default_factory=dict)
    onboarding_done: bool
    require_2fa: bool = False
    is_org_owner: bool = False
    created_at: datetime
    updated_at: datetime


class AssetUploadResponse(BaseModel):
    url: str
    category: str
    object_key: str | None = None
