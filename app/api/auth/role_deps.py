from fastapi import Depends, HTTPException, status

from app.api.auth.dependencies import get_current_user
from app.infrastructure.database.models.user import User


def require_role(*allowed_roles: str):
    async def _check(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required roles: {', '.join(sorted(allowed_roles))}",
            )
        return current_user
    return _check
