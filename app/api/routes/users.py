import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.passwords import hash_password, verify_password
from app.api.auth.role_deps import require_role
from app.api.errors import bad_request, conflict, not_found, rate_limited
from app.api.schemas.user import (
    InviteUserRequest,
    InviteUserResponse,
    MeUpdateRequest,
    PasswordUpdateRequest,
    UpdateUserRoleRequest,
    UserResponse,
)
from app.infrastructure.audit import log_audit
from app.infrastructure.database.models.invitation import Invitation
from app.infrastructure.database.models.organization import Organization
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.user_repository import UserRepository
from app.infrastructure.database.session import get_db
from app.services.email_queue import queue_email
from app.infrastructure.redis import tokens as redis_tokens
from app.infrastructure.redis.client import get_redis
from app.infrastructure.redis.keys import invite_rl_invitation, invite_rl_org
from app.infrastructure.redis.rate_limit import (
    INVITE_ORG_PER_HOUR,
    INVITE_RESEND_COOLDOWN_SEC,
    enforce_fixed_window_limit,
)

router = APIRouter(prefix="/users", tags=["Users"])


def _parse_uuid(value: str, field_name: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid {field_name}") from exc


def _to_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        org_id=user.org_id,
        email=user.email,
        full_name=user.full_name,
        profile_image_url=user.profile_image_url,
        role=user.role,
        is_active=user.is_active,
        is_verified=user.is_verified,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)) -> UserResponse:
    """Return the currently authenticated user's profile. Prefer `GET /auth/me` which also returns org details."""
    return _to_response(current_user)


@router.put("/me", response_model=UserResponse)
async def update_me(
    body: MeUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Update the current user's display name or avatar URL.

    To upload an image file directly use `POST /users/me/avatar` instead.
    Pass `avatar_url` to set an externally-hosted image (e.g. from OAuth providers).
    """
    if body.full_name is not None:
        current_user.full_name = body.full_name
    if body.avatar_url is not None:
        current_user.profile_image_url = body.avatar_url
    await db.commit()
    await db.refresh(current_user)
    return _to_response(current_user)


_ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_MAX_AVATAR_BYTES = 5 * 1024 * 1024  # 5 MB


@router.post("/me/avatar", response_model=UserResponse)
async def upload_my_avatar(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Upload a profile photo for the current user.

    Send as `multipart/form-data` with the image in the `file` field.
    Accepted formats: JPEG, PNG, WebP, GIF. Max size: 5 MB.
    On success, `profile_image_url` in all subsequent `/auth/me` and `/users/me`
    responses will reflect the new URL.

    Requires `ASSETS_S3_BUCKET` to be configured; returns 503 if S3 is not set up.
    """
    content_type = file.content_type or ""
    if content_type not in _ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_FILE_TYPE", "message": f"File must be an image (jpeg/png/webp/gif), got {content_type!r}"},
        )
    data = await file.read(_MAX_AVATAR_BYTES + 1)
    if len(data) > _MAX_AVATAR_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"code": "FILE_TOO_LARGE", "message": "Avatar must be 5 MB or smaller"},
        )
    try:
        from app.infrastructure.external_services.s3_assets import upload_bytes
        url, _ = await upload_bytes(
            data,
            org_id=current_user.org_id,
            category="profile",
            filename=file.filename or "avatar",
            content_type=content_type,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "STORAGE_UNAVAILABLE", "message": str(exc)},
        ) from exc
    current_user.profile_image_url = url
    await db.commit()
    await db.refresh(current_user)
    return _to_response(current_user)


@router.put("/me/password")
async def update_me_password(
    body: PasswordUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Change the current user's password. Requires the existing password for verification.

    Returns 422 if `current_password` is wrong or `new_password` is shorter than 8 characters.
    Not available for OAuth-only accounts (Google sign-in with no password set).
    """
    if not current_user.password_hash:
        raise bad_request("NO_PASSWORD", "OAuth-only account has no password")
    if not verify_password(body.current_password, current_user.password_hash):
        raise bad_request("BAD_REQUEST", "Current password is wrong")
    if len(body.new_password) < 8:
        raise bad_request("VALIDATION_ERROR", "Password too short")
    current_user.password_hash = hash_password(body.new_password)
    await db.commit()
    return {"message": "Password updated"}


@router.get("", response_model=list[UserResponse])
async def list_users(
    current_user: User = Depends(require_role("admin", "manager")),
    db: AsyncSession = Depends(get_db),
) -> list[UserResponse]:
    """List all active and inactive users in the org. Requires admin or manager role."""
    users = await UserRepository(db).list_by_org(current_user.org_id)
    return [_to_response(user) for user in users]


@router.post("/invite", response_model=InviteUserResponse, status_code=201)
async def invite_user(
    body: InviteUserRequest,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> InviteUserResponse:
    """Invite a teammate by email with a specific role. Requires admin role.

    Sends an email with a 72-hour accept link pointing to `{FRONTEND_URL}/auth/accept-invite?token=...`.
    The invitee calls `POST /auth/accept-invite` with the token, their name, and a password to
    create their account. Roles available: `manager`, `analyst`, `viewer` (admin cannot be invited).
    Returns 409 if a pending invitation already exists for that email.
    """
    r = await get_redis()
    await enforce_fixed_window_limit(
        r,
        key=invite_rl_org(current_user.org_id),
        limit=INVITE_ORG_PER_HOUR,
        window_sec=3600,
        message="Too many invitations sent for this organization. Try again later.",
    )
    if body.role == "admin":
        raise bad_request("BAD_REQUEST", "Cannot invite as admin via this endpoint")
    repo = UserRepository(db)
    existing = await repo.get_by_email(str(body.email))
    if existing and existing.org_id == current_user.org_id:
        raise conflict("ALREADY_IN_ORG", "Email already belongs to a user in this org")
    pending = await db.execute(
        select(Invitation).where(
            Invitation.org_id == current_user.org_id,
            Invitation.email == str(body.email),
            Invitation.accepted_at.is_(None),
        )
    )
    if pending.scalar_one_or_none():
        raise conflict("INVITATION_PENDING", "Pending invitation already exists for this email")

    token = secrets.token_hex(32)
    exp = datetime.now(timezone.utc) + timedelta(hours=72)
    inv = Invitation(
        org_id=current_user.org_id,
        invited_by=current_user.id,
        email=str(body.email),
        role=body.role,
        token=token,
        expires_at=exp,
    )
    db.add(inv)
    await db.flush()
    await log_audit(
        db,
        org_id=current_user.org_id,
        user_id=current_user.id,
        action="user.invited",
        resource="invitation",
        resource_id=inv.id,
        metadata={"email": inv.email, "role": inv.role},
    )
    await db.commit()
    await db.refresh(inv)
    org = await db.get(Organization, current_user.org_id)
    await queue_email(
        "invitation",
        to=inv.email,
        token=token,
        invited_by=current_user.full_name or current_user.email,
        org_name=org.name if org else "your organization",
        role=inv.role,
    )
    return InviteUserResponse(
        invitation_id=inv.id,
        email=inv.email,
        role=inv.role,
        expires_at=inv.expires_at,
    )


@router.get("/invitations")
async def list_invitations(
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List all pending (not yet accepted, not yet expired) invitations. Requires admin role."""
    now = datetime.now(timezone.utc)
    r = await db.execute(
        select(Invitation).where(
            Invitation.org_id == current_user.org_id,
            Invitation.accepted_at.is_(None),
            Invitation.expires_at > now,
        )
    )
    rows = list(r.scalars().all())
    return {
        "invitations": [
            {
                "id": str(i.id),
                "email": i.email,
                "role": i.role,
                "invited_by": str(i.invited_by),
                "expires_at": i.expires_at.isoformat(),
                "created_at": i.created_at.isoformat(),
            }
            for i in rows
        ]
    }


@router.post("/invitations/{invitation_id}/resend", response_model=InviteUserResponse)
async def resend_invitation(
    invitation_id: UUID,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> InviteUserResponse:
    """Resend an invitation email and reset the 72-hour expiry window. Requires admin role.

    Generates a fresh token (invalidating the old link), updates the expiry, and
    re-sends the email. Returns 404 if the invitation doesn't exist or was already accepted.
    """
    inv = await db.get(Invitation, invitation_id)
    if not inv or inv.org_id != current_user.org_id or inv.accepted_at is not None:
        raise not_found("Invitation not found or already accepted")

    r = await get_redis()
    await enforce_fixed_window_limit(
        r,
        key=invite_rl_org(current_user.org_id),
        limit=INVITE_ORG_PER_HOUR,
        window_sec=3600,
        message="Too many invitations sent for this organization. Try again later.",
    )
    cooldown_key = invite_rl_invitation(invitation_id)
    if r is not None and await r.get(cooldown_key):
        raise rate_limited("Please wait before resending this invitation")

    inv.token = secrets.token_hex(32)
    inv.expires_at = datetime.now(timezone.utc) + timedelta(hours=72)
    await db.flush()
    org = await db.get(Organization, current_user.org_id)
    await queue_email(
        "invitation",
        to=inv.email,
        token=inv.token,
        invited_by=current_user.full_name or current_user.email,
        org_name=org.name if org else "your organization",
        role=inv.role,
    )
    if r is not None:
        await r.set(cooldown_key, "1", ex=INVITE_RESEND_COOLDOWN_SEC)
    await db.commit()
    await db.refresh(inv)
    return InviteUserResponse(
        invitation_id=inv.id,
        email=inv.email,
        role=inv.role,
        expires_at=inv.expires_at,
    )


@router.delete("/invitations/{invitation_id}", status_code=204)
async def revoke_invitation(
    invitation_id: UUID,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Revoke a pending invitation, invalidating its token. Requires admin role.

    Returns 404 if the invitation does not exist or has already been accepted.
    """
    inv = await db.get(Invitation, invitation_id)
    if not inv or inv.org_id != current_user.org_id or inv.accepted_at is not None:
        raise not_found("Invitation not found or already accepted")
    await db.delete(inv)
    await db.commit()


@router.patch("/{user_id}/role", response_model=UserResponse)
async def update_user_role(
    user_id: str,
    body: UpdateUserRoleRequest,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Change a user's role. Requires admin role.

    Cannot demote yourself, cannot remove the last admin.
    Available roles: `admin`, `manager`, `analyst`, `viewer`.
    """
    repo = UserRepository(db)
    target = await repo.get_by_id(_parse_uuid(user_id, "user_id"))
    if not target or target.org_id != current_user.org_id:
        raise not_found("User not found")
    if target.id == current_user.id:
        raise bad_request("CANNOT_SELF_DEMOTE", "Cannot change your own role")
    if target.role == "admin" and body.role != "admin" and await repo.count_admins(current_user.org_id) <= 1:
        raise bad_request("LAST_ADMIN", "Cannot remove last admin")

    old_role = target.role
    target.role = body.role
    await log_audit(
        db,
        org_id=current_user.org_id,
        user_id=current_user.id,
        action="user.role_changed",
        resource="user",
        resource_id=target.id,
        metadata={"email": target.email, "from_role": old_role, "to_role": body.role},
    )
    await db.commit()
    return _to_response(target)


@router.delete("/{user_id}", status_code=204)
async def deactivate_user(
    user_id: str,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate (soft-delete) a user. The account is disabled but data is retained. Requires admin role.

    Cannot deactivate yourself. Cannot deactivate the last admin.
    Deactivated users receive a 403 on their next request.
    """
    repo = UserRepository(db)
    target = await repo.get_by_id(_parse_uuid(user_id, "user_id"))
    if not target or target.org_id != current_user.org_id:
        raise not_found("User not found")
    if target.id == current_user.id:
        raise bad_request("CANNOT_DEACTIVATE_SELF", "Cannot deactivate yourself")
    if target.role == "admin" and await repo.count_admins(current_user.org_id) <= 1:
        raise bad_request("LAST_ADMIN", "Cannot deactivate last admin")

    target.is_active = False
    await log_audit(
        db,
        org_id=current_user.org_id,
        user_id=current_user.id,
        action="user.deactivated",
        resource="user",
        resource_id=target.id,
        metadata={"email": target.email},
    )
    await db.commit()
