from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


Role = Literal["admin", "ops_manager"]


class UserResponse(BaseModel):
    id: UUID
    org_id: UUID
    email: str
    role: str
    created_at: datetime


class InviteUserRequest(BaseModel):
    email: EmailStr
    role: Role = "ops_manager"


class InviteUserResponse(BaseModel):
    user: UserResponse
    temporary_password: str = Field(
        ...,
        description="Hackathon-only bootstrap password. Replace with email invite flow before production.",
    )


class UpdateUserRoleRequest(BaseModel):
    role: Role
