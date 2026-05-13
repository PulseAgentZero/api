import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.passwords import hash_password, verify_password
from app.api.auth.role_deps import require_role
from app.api.errors import bad_request, conflict, not_found
from app.api.schemas.user import (
    InviteUserRequest,
    InviteUserResponse,
    MeUpdateRequest,
    PasswordUpdateRequest,
    UpdateUserRoleRequest,
    UserResponse,
)
from app.infrastructure.database.models.invitation import Invitation
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.user_repository import UserRepository
from app.infrastructure.database.session import get_db
from app.infrastructure.redis import tokens as redis_tokens

router = APIRouter(prefix="/users", tags=["users"])


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
        role=user.role,
        is_active=user.is_active,
        is_verified=user.is_verified,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)) -> UserResponse:
    return _to_response(current_user)


@router.put("/me", response_model=UserResponse)
async def update_me(
    body: MeUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    if body.full_name is not None:
        current_user.full_name = body.full_name
    await db.commit()
    await db.refresh(current_user)
    return _to_response(current_user)


@router.put("/me/password")
async def update_me_password(
    body: PasswordUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
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
    users = await UserRepository(db).list_by_org(current_user.org_id)
    return [_to_response(user) for user in users]


@router.post("/invite", response_model=InviteUserResponse, status_code=201)
async def invite_user(
    body: InviteUserRequest,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> InviteUserResponse:
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
    await db.commit()
    await db.refresh(inv)
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


@router.delete("/invitations/{invitation_id}", status_code=204)
async def revoke_invitation(
    invitation_id: UUID,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> None:
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
    repo = UserRepository(db)
    target = await repo.get_by_id(_parse_uuid(user_id, "user_id"))
    if not target or target.org_id != current_user.org_id:
        raise not_found("User not found")
    if target.id == current_user.id:
        raise bad_request("CANNOT_SELF_DEMOTE", "Cannot change your own role")
    if target.role == "admin" and body.role != "admin" and await repo.count_admins(current_user.org_id) <= 1:
        raise bad_request("LAST_ADMIN", "Cannot remove last admin")

    target.role = body.role
    await db.commit()
    return _to_response(target)


@router.delete("/{user_id}", status_code=204)
async def deactivate_user(
    user_id: str,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> None:
    repo = UserRepository(db)
    target = await repo.get_by_id(_parse_uuid(user_id, "user_id"))
    if not target or target.org_id != current_user.org_id:
        raise not_found("User not found")
    if target.id == current_user.id:
        raise bad_request("CANNOT_DEACTIVATE_SELF", "Cannot deactivate yourself")
    if target.role == "admin" and await repo.count_admins(current_user.org_id) <= 1:
        raise bad_request("LAST_ADMIN", "Cannot deactivate last admin")

    target.is_active = False
    await db.commit()
