from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class RecommendationResponse(BaseModel):
    id: UUID
    org_id: UUID
    entity_id: str | None
    entity_label: str | None
    type: str | None
    urgency: str | None
    title: str | None
    reasoning: str | None
    suggested_action: str | None
    status: str
    actioned_by: UUID | None
    actioned_at: datetime | None
    created_at: datetime


class UpdateRecommendationRequest(BaseModel):
    status: Literal["active", "actioned", "dismissed"]
