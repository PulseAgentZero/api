"""Audit log listing (BACKEND_ROUTES §18)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.role_deps import require_role
from app.api.dependencies.plan_gate import require_feature
from app.config.settings import Settings
from app.infrastructure.database.models.audit_log import AuditLog
from app.infrastructure.database.models.user import User
from app.infrastructure.database.session import get_db

router = APIRouter(prefix="/audit-logs", tags=["Audit Logs"])


@router.get("")
async def list_audit_logs(
    action: str | None = None,
    user_id: UUID | None = None,
    resource: str | None = None,
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_feature(db, current_user.org_id, "audit_log")
    conds = [AuditLog.org_id == current_user.org_id]
    if action:
        conds.append(AuditLog.action == action)
    if user_id:
        conds.append(AuditLog.user_id == user_id)
    if resource:
        conds.append(AuditLog.resource == resource)
    if from_:
        conds.append(AuditLog.created_at >= from_)
    if to:
        conds.append(AuditLog.created_at <= to)

    total = await db.scalar(select(func.count()).select_from(AuditLog).where(*conds)) or 0
    stmt = (
        select(AuditLog)
        .where(*conds)
        .order_by(AuditLog.created_at.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    out = []
    for log in rows:
        u = await db.get(User, log.user_id) if log.user_id else None
        out.append(
            {
                "id": str(log.id),
                "action": log.action,
                "resource": log.resource,
                "resource_id": str(log.resource_id) if log.resource_id else None,
                "user": (
                    {"id": str(u.id), "email": u.email, "full_name": u.full_name}
                    if u
                    else None
                ),
                "metadata": log.metadata_,
                "ip_address": str(log.ip_address) if log.ip_address else None,
                "created_at": log.created_at.isoformat(),
            }
        )
    return {"logs": out, "total": int(total), "page": page, "limit": limit}
