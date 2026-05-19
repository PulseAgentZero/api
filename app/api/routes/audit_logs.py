"""Audit log listing and export (Pro / licensed orgs)."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.role_deps import require_role
from app.api.dependencies.plan_gate import require_feature
from app.infrastructure.database.models.audit_log import AuditLog
from app.infrastructure.database.models.user import User
from app.infrastructure.database.session import get_db

router = APIRouter(prefix="/audit-logs", tags=["Audit Logs"])

_EXPORT_MAX_ROWS = 10_000


def _audit_conditions(
    org_id: UUID,
    *,
    action: str | None,
    user_id: UUID | None,
    resource: str | None,
    from_: datetime | None,
    to: datetime | None,
) -> list:
    conds = [AuditLog.org_id == org_id]
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
    return conds


async def _serialize_audit_row(db: AsyncSession, log: AuditLog) -> dict:
    u = await db.get(User, log.user_id) if log.user_id else None
    return {
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
        "user_agent": log.user_agent,
        "created_at": log.created_at.isoformat(),
    }


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
    """List audit events for this organization (who did what, when). Requires Pro / audit_log feature."""
    await require_feature(db, current_user.org_id, "audit_log")
    conds = _audit_conditions(
        current_user.org_id,
        action=action,
        user_id=user_id,
        resource=resource,
        from_=from_,
        to=to,
    )
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
    out = [await _serialize_audit_row(db, log) for log in rows]
    return {"logs": out, "total": int(total), "page": page, "limit": limit}


@router.get("/export")
async def export_audit_logs(
    action: str | None = None,
    user_id: UUID | None = None,
    resource: str | None = None,
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Download audit log as CSV (same filters as list; max 10,000 rows). Admin only."""
    await require_feature(db, current_user.org_id, "audit_log")
    conds = _audit_conditions(
        current_user.org_id,
        action=action,
        user_id=user_id,
        resource=resource,
        from_=from_,
        to=to,
    )
    stmt = (
        select(AuditLog)
        .where(*conds)
        .order_by(AuditLog.created_at.desc())
        .limit(_EXPORT_MAX_ROWS)
    )
    result = await db.execute(stmt)
    rows = list(result.scalars().all())

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "id",
            "created_at",
            "action",
            "resource",
            "resource_id",
            "user_id",
            "user_email",
            "user_full_name",
            "ip_address",
            "user_agent",
            "metadata",
        ]
    )
    for log in rows:
        u = await db.get(User, log.user_id) if log.user_id else None
        writer.writerow(
            [
                str(log.id),
                log.created_at.isoformat(),
                log.action,
                log.resource or "",
                str(log.resource_id) if log.resource_id else "",
                str(log.user_id) if log.user_id else "",
                u.email if u else "",
                u.full_name if u else "",
                str(log.ip_address) if log.ip_address else "",
                log.user_agent or "",
                str(log.metadata_ or {}),
            ]
        )

    filename = f"pulse-audit-logs-{datetime.utcnow().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
