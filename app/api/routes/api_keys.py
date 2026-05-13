"""Programmatic API keys (BACKEND_ROUTES §15)."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.role_deps import require_role
from app.api.dependencies.plan_gate import require_feature
from app.api.errors import not_found
from app.infrastructure.database.models.api_key import ApiKey
from app.infrastructure.database.models.user import User
from app.infrastructure.database.session import get_db

router = APIRouter(prefix="/api-keys", tags=["API Keys"])


class ApiKeyCreate(BaseModel):
    name: str = Field(..., min_length=1)
    scope: str = Field("read", pattern="^(read|write)$")
    expires_at: datetime | None = None


@router.get("")
async def list_api_keys(
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_feature(db, current_user.org_id, "api_keys")
    result = await db.execute(
        select(ApiKey).where(
            ApiKey.org_id == current_user.org_id,
            ApiKey.revoked_at.is_(None),
        )
    )
    rows = list(result.scalars().all())
    return {
        "api_keys": [
            {
                "id": str(k.id),
                "name": k.name,
                "key_prefix": k.key_prefix,
                "scope": k.scope,
                "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
                "expires_at": k.expires_at.isoformat() if k.expires_at else None,
                "created_at": k.created_at.isoformat(),
            }
            for k in rows
        ]
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_api_key(
    body: ApiKeyCreate,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_feature(db, current_user.org_id, "api_keys")
    prefix = "pk_live_" if body.scope == "write" else "pk_read_"
    raw = prefix + secrets.token_urlsafe(32)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    key_prefix = raw[:12]
    row = ApiKey(
        org_id=current_user.org_id,
        created_by=current_user.id,
        name=body.name,
        key_prefix=key_prefix,
        key_hash=digest,
        scope=body.scope,
        expires_at=body.expires_at,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {
        "id": str(row.id),
        "name": row.name,
        "key": raw,
        "key_prefix": row.key_prefix,
        "scope": row.scope,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "created_at": row.created_at.isoformat(),
    }


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    key_id: UUID,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> None:
    await require_feature(db, current_user.org_id, "api_keys")
    row = await db.get(ApiKey, key_id)
    if not row or row.org_id != current_user.org_id:
        raise not_found()
    row.revoked_at = datetime.now(timezone.utc)
    await db.commit()
