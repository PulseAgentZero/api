from uuid import UUID

from pydantic import BaseModel, EmailStr


class SignupRequest(BaseModel):
    email: EmailStr
    password: str
    organization_name: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: UUID
    email: str
    role: str
    created_at: str


class OrgOut(BaseModel):
    id: UUID
    name: str
    industry: str | None
    onboarding_done: bool


class MeResponse(BaseModel):
    user: UserOut
    organization: OrgOut
