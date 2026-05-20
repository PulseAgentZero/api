from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.org_helpers import is_org_owner, user_can_manage_org_security
from app.api.errors import forbidden
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.organization_repository import (
    OrganizationRepository,
)
from app.infrastructure.database.session import get_db


def require_org_owner():
    async def _check(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> User:
        org = await OrganizationRepository(db).get_by_id(current_user.org_id)
        if not is_org_owner(current_user, org):
            raise forbidden(
                "OWNER_REQUIRED",
                "Only the organization owner can perform this action.",
            )
        return current_user

    return _check


def require_org_security_manager():
    """Owner or admin — manage org security settings."""

    async def _check(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> User:
        org = await OrganizationRepository(db).get_by_id(current_user.org_id)
        if not user_can_manage_org_security(current_user, org):
            raise forbidden(
                "ACCESS_DENIED",
                "Only the organization owner or an admin can change security settings.",
            )
        return current_user

    return _check


def require_role(*allowed_roles: str):
    async def _check(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required roles: {', '.join(sorted(allowed_roles))}",
            )
        return current_user
    return _check
