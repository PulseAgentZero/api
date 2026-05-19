"""In-app notifications (BACKEND_ROUTES §13)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.infrastructure.database.models.org_notification import OrgNotification
from app.infrastructure.database.models.user import User
from app.infrastructure.database.session import get_db

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get("")
async def list_notifications(
    unread_only: bool = Query(False),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    base_conds = [
        OrgNotification.org_id == current_user.org_id,
        (OrgNotification.user_id == current_user.id) | (OrgNotification.user_id.is_(None)),
    ]
    if unread_only:
        base_conds.append(OrgNotification.read_at.is_(None))

    total = await db.scalar(
        select(func.count()).select_from(OrgNotification).where(*base_conds)
    ) or 0

    stmt = (
        select(OrgNotification)
        .where(*base_conds)
        .order_by(OrgNotification.created_at.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    unread = await db.scalar(
        select(func.count())
        .select_from(OrgNotification)
        .where(
            OrgNotification.org_id == current_user.org_id,
            OrgNotification.read_at.is_(None),
            (OrgNotification.user_id == current_user.id) | (OrgNotification.user_id.is_(None)),
        )
    )
    return {
        "notifications": [
            {
                "id": str(n.id),
                "title": n.title,
                "body": n.body,
                "type": n.type,
                "action_url": n.action_url,
                "source": n.source,
                "read_at": n.read_at.isoformat() if n.read_at else None,
                "created_at": n.created_at.isoformat(),
            }
            for n in rows
        ],
        "unread_count": int(unread or 0),
        "total": int(total),
        "page": page,
        "limit": limit,
    }


@router.post("/{notification_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_notification_read(
    notification_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(
        update(OrgNotification)
        .where(
            OrgNotification.id == notification_id,
            OrgNotification.org_id == current_user.org_id,
            (OrgNotification.user_id == current_user.id) | (OrgNotification.user_id.is_(None)),
            OrgNotification.read_at.is_(None),
        )
        .values(read_at=func.now())
    )
    await db.commit()


@router.post("/read-all")
async def mark_all_read(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    res = await db.execute(
        update(OrgNotification)
        .where(
            OrgNotification.org_id == current_user.org_id,
            OrgNotification.read_at.is_(None),
            (OrgNotification.user_id == current_user.id) | (OrgNotification.user_id.is_(None)),
        )
        .values(read_at=func.now())
    )
    await db.commit()
    return {"marked_read": res.rowcount or 0}
