import hashlib
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.jwt_utils import decode_access_token
from app.infrastructure.database.models.api_key import ApiKey
from app.infrastructure.database.repositories.user_repository import UserRepository
from app.infrastructure.database.session import get_db

_bearer = HTTPBearer(auto_error=False)


async def _user_from_api_key(db: AsyncSession, raw_key: str):
    digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    result = await db.execute(
        select(ApiKey).where(ApiKey.key_hash == digest, ApiKey.revoked_at.is_(None))
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    if row.expires_at is not None and row.expires_at < datetime.now(timezone.utc):
        return None
    user = await UserRepository(db).get_by_id(row.created_by)
    if user is None or user.org_id != row.org_id:
        return None
    row.last_used_at = datetime.now(timezone.utc)
    await db.flush()
    return user


async def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
):
    if x_api_key:
        return await _user_from_api_key(db, x_api_key)
    if credentials is None:
        return None
    payload = decode_access_token(credentials.credentials)
    if payload is None:
        return None
    if payload.get("type") == "refresh":
        return None
    user = await UserRepository(db).get_by_id(uuid.UUID(payload["sub"]))
    return user


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
):
    if x_api_key:
        user = await _user_from_api_key(db, x_api_key)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "INVALID_TOKEN", "message": "Invalid API key"},
            )
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "ACCOUNT_DEACTIVATED", "message": "Account deactivated"},
            )
        return user
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_TOKEN", "message": "Missing authorization header"},
        )
    payload = decode_access_token(credentials.credentials)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "TOKEN_EXPIRED", "message": "Invalid or expired token"},
        )
    if payload.get("type") == "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_TOKEN", "message": "Refresh token cannot be used for this endpoint"},
        )
    user = await UserRepository(db).get_by_id(uuid.UUID(payload["sub"]))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_TOKEN", "message": "User not found"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "ACCOUNT_DEACTIVATED", "message": "Account deactivated"},
        )
    return user
