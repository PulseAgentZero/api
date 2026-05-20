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


class MfaRequiredResponse(BaseModel):
    status: str = "mfa_required"
    mfa_token: str
    user: dict


class TotpSetupResponse(BaseModel):
    secret: str
    otpauth_uri: str


class TotpEnableRequest(BaseModel):
    code: str = Field(min_length=6, max_length=16)


class TotpDisableRequest(BaseModel):
    code: str = Field(min_length=6, max_length=32)
    password: str | None = None


class MfaVerifyRequest(BaseModel):
    mfa_token: str
    code: str = Field(min_length=6, max_length=32)


class DeleteAccountRequest(BaseModel):
    password: str | None = None
    totp_code: str | None = None


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
    totp_enabled: bool = False
    is_org_owner: bool = False


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
    require_2fa: bool = False


class MeResponse(BaseModel):
    user: UserOut
    org: OrgOut


class GoogleLinkRequest(BaseModel):
    link_token: str
    password: str | None = None


class GoogleLinkCancelRequest(BaseModel):
    link_token: str


class GoogleCompleteSignupRequest(BaseModel):
    pending_token: str
    org_name: str = Field(min_length=1)
    full_name: str = ""


class InvitePreviewResponse(BaseModel):
    email: str
    org_name: str
    role: str
    expires_at: str
