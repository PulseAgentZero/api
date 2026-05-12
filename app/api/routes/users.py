import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.passwords import hash_password
from app.api.auth.role_deps import require_role
from app.api.schemas.user import (
    InviteUserRequest,
    InviteUserResponse,
    UpdateUserRoleRequest,
    UserResponse,
)
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.user_repository import UserRepository
from app.infrastructure.database.session import get_db

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
        role=user.role,
        created_at=user.created_at,
    )


@router.get("", response_model=list[UserResponse])
async def list_users(
    current_user: User = Depends(require_role("admin")),
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
    repo = UserRepository(db)
    existing = await repo.get_by_email(str(body.email))
    if existing:
        if existing.org_id == current_user.org_id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already exists in this organization")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    temporary_password = secrets.token_urlsafe(18)
    user = await repo.create(
        org_id=current_user.org_id,
        email=str(body.email),
        password_hash=hash_password(temporary_password),
        role=body.role,
    )
    await db.commit()
    return InviteUserResponse(user=_to_response(user), temporary_password=temporary_password)


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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if target.id == current_user.id and body.role != "admin":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot remove your own admin role")
    if target.role == "admin" and body.role != "admin" and await repo.count_admins(current_user.org_id) <= 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Organization must have at least one admin")

    target.role = body.role
    await db.flush()
    await db.commit()
    return _to_response(target)


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    repo = UserRepository(db)
    target = await repo.get_by_id(_parse_uuid(user_id, "user_id"))
    if not target or target.org_id != current_user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if target.id == current_user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete your own user")
    if target.role == "admin" and await repo.count_admins(current_user.org_id) <= 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Organization must have at least one admin")

    await repo.delete(target.id)
    await db.commit()
