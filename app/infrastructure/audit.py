"""Immutable audit log writes (Pro / licensed orgs)."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.infrastructure.database.base import utcnow
from app.infrastructure.database.models.audit_log import AuditLog
from app.infrastructure.database.models.organization import Organization

logger = logging.getLogger(__name__)


async def log_audit(
    db: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID | None,
    action: str,
    resource: str | None = None,
    resource_id: UUID | None = None,
    metadata: dict[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Best-effort audit row; skips if org is not on a plan that includes audit."""
    try:
        org = await db.get(Organization, org_id)
        if org is None:
            return
        plan = (org.plan or "free").lower()
        if settings.DEPLOYMENT_MODE == "cloud" and plan not in ("pro", "enterprise"):
            return
        row = AuditLog(
            org_id=org_id,
            user_id=user_id,
            action=action,
            resource=resource,
            resource_id=resource_id,
            metadata_=metadata or {},
            ip_address=ip_address,
            user_agent=user_agent,
            created_at=utcnow(),
        )
        db.add(row)
        await db.flush()
    except Exception:
        logger.exception("audit log write failed for action=%s org=%s", action, org_id)
