import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt

from app.config.settings import settings


def parse_uuid_sub(payload: dict[str, Any] | None) -> uuid.UUID | None:
    """Return user id from JWT payload ``sub`` if it is a valid UUID string."""
    if not payload:
        return None
    sub = payload.get("sub")
    if not isinstance(sub, str):
        return None
    try:
        return uuid.UUID(sub)
    except ValueError:
        return None


def parse_uuid_loose(value: Any) -> uuid.UUID | None:
    """Parse a UUID from common wire formats (string or UUID)."""
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


def create_access_token(user_id: uuid.UUID, org_id: uuid.UUID, role: str, email: str) -> str:
    payload = {
        "sub": str(user_id),
        "org_id": str(org_id),
        "role": role,
        "email": email,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc)
        + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(user_id: uuid.UUID) -> str:
    payload = {
        "sub": str(user_id),
        "type": "refresh",
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(days=7),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
