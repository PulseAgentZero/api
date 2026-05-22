from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=4000)
    conversation_id: UUID | None = None
    stream: bool = False


class ChatResponse(BaseModel):
    reply: str
    conversation_id: UUID | None = None
    tools_called: list[str] | None = None
    # Raw, uncondensed payloads from UI-facing tools — keyed by tool name.
    # Frontend uses these to render structured cards (intake questions,
    # plan preview, change diff). Absent when no UI tool ran this turn.
    artifacts: dict[str, Any] | None = None


class ConversationListItem(BaseModel):
    id: UUID
    message_count: int
    created_at: datetime
    updated_at: datetime


class ConversationResponse(BaseModel):
    id: UUID
    org_id: UUID
    user_id: UUID | None
    messages: list[dict]
    created_at: datetime
    updated_at: datetime
