"""Webhook delivery log (BACKEND_ROUTES §14)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.role_deps import require_role
from app.api.dependencies.plan_gate import require_feature
from app.infrastructure.database.models.user import User
from app.infrastructure.database.models.webhook_delivery import WebhookDelivery
from app.infrastructure.database.session import get_db

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


@router.get("/deliveries")
async def list_deliveries(
    channel_id: UUID | None = None,
    status_filter: str | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_feature(db, current_user.org_id, "webhook_deliveries")
    stmt = select(WebhookDelivery).where(WebhookDelivery.org_id == current_user.org_id)
    if channel_id:
        stmt = stmt.where(WebhookDelivery.channel_id == channel_id)
    if status_filter:
        stmt = stmt.where(WebhookDelivery.status == status_filter)
    stmt = stmt.order_by(WebhookDelivery.created_at.desc()).offset((page - 1) * limit).limit(limit)
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    return {
        "deliveries": [
            {
                "id": str(d.id),
                "channel_id": str(d.channel_id),
                "event_type": d.event_type,
                "status": d.status,
                "attempts": d.attempts,
                "response_status": d.response_status,
                "last_attempt_at": d.last_attempt_at.isoformat() if d.last_attempt_at else None,
                "created_at": d.created_at.isoformat(),
            }
            for d in rows
        ]
    }


@router.post("/deliveries/{delivery_id}/retry")
async def retry_delivery(
    delivery_id: UUID,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_feature(db, current_user.org_id, "webhook_deliveries")
    d = await db.get(WebhookDelivery, delivery_id)
    if not d or d.org_id != current_user.org_id:
        from app.api.errors import not_found

        raise not_found()
    if d.status != "failed":
        from app.api.errors import bad_request

        raise bad_request("BAD_REQUEST", "Delivery is not in failed status")
    from app.services.webhook_dispatch import execute_pending_delivery

    d.status = "pending"
    d.next_retry_at = None
    await execute_pending_delivery(db, d)
    await db.commit()
    return {"message": "Retry completed", "status": d.status, "attempts": d.attempts}
