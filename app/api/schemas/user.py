from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

Role = Literal["admin", "manager", "analyst", "viewer"]


class UserResponse(BaseModel):
    id: UUID
    org_id: UUID
    email: str
    full_name: str
    profile_image_url: str | None = None
    role: str
    is_active: bool
    is_verified: bool
    last_login_at: datetime | None
    created_at: datetime


class MeUpdateRequest(BaseModel):
    full_name: str | None = None
    avatar_url: str | None = None


class PasswordUpdateRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class InviteUserRequest(BaseModel):
    email: EmailStr
    role: Literal["manager", "analyst", "viewer"]


class InviteUserResponse(BaseModel):
    invitation_id: UUID
    email: str
    role: str
    expires_at: datetime


class UpdateUserRoleRequest(BaseModel):
    role: Literal["admin", "manager", "analyst", "viewer"]
