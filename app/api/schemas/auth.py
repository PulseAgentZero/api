from typing import Any
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str = ""
    org_name: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8)


class AcceptInviteRequest(BaseModel):
    token: str
    full_name: str
    password: str = Field(min_length=8)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: dict | None = None
    org: dict | None = None


class UserOut(BaseModel):
    id: UUID
    email: str
    full_name: str
    role: str
    is_verified: bool
    is_active: bool | None = None
    last_login_at: str | None = None
    created_at: str
    org_id: UUID | None = None
    profile_image_url: str | None = None


class OrgOut(BaseModel):
    id: UUID
    name: str
    slug: str | None = None
    industry: str | None
    plan: str | None = None
    onboarding_done: bool
    created_at: str | None = None
    logo_url: str | None = None
    tour_guide: dict[str, Any] = Field(default_factory=dict)


class MeResponse(BaseModel):
    user: UserOut
    org: OrgOut
