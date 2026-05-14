"""Self-hosted license entitlements for plan gating (LICENSE_SYSTEM.md)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.infrastructure.database.models.license_key import LicenseKey
from app.infrastructure.license.jwt_verify import decode_license_jwt_payload, jwt_expired

logger = logging.getLogger(__name__)


def _validation_deadline(row: LicenseKey) -> datetime | None:
    """End of grace period: explicit cache field, or last server validation + grace days."""
    if row.validation_cached_until:
        return row.validation_cached_until
    if row.last_validated_at:
        return row.last_validated_at + timedelta(days=settings.LICENSE_OFFLINE_GRACE_DAYS)
    return None


@dataclass(frozen=True)
class SelfHostedEntitlements:
    plan: str
    features: list[str]
    locked: bool
    lock_reason: str | None
    validation_cached_until: datetime | None


async def resolve_self_hosted_entitlements(
    db: AsyncSession, org_id: UUID
) -> SelfHostedEntitlements:
    """Combine DB row, offline JWT verification, and validation cache window."""
    r = await db.execute(select(LicenseKey).where(LicenseKey.org_id == org_id))
    row = r.scalar_one_or_none()
    now = datetime.now(timezone.utc)

    if row is None or not row.is_active:
        return SelfHostedEntitlements(
            plan="free",
            features=[],
            locked=False,
            lock_reason=None,
            validation_cached_until=None,
        )

    plan = (row.plan or "free").lower()
    features = [str(x).lower() for x in (row.features or [])]

    if settings.PULSE_LICENSE_PUBLIC_KEY:
        try:
            payload = decode_license_jwt_payload(row.license_key)
        except jwt.PyJWTError as exc:
            logger.warning("License JWT verification failed for org %s: %s", org_id, exc)
            return SelfHostedEntitlements(
                plan="free",
                features=[],
                locked=True,
                lock_reason="INVALID_LICENSE_SIGNATURE",
                validation_cached_until=row.validation_cached_until,
            )
        if jwt_expired(payload, now=now):
            return SelfHostedEntitlements(
                plan="free",
                features=[],
                locked=True,
                lock_reason="LICENSE_EXPIRED",
                validation_cached_until=row.validation_cached_until,
            )
        if payload.get("plan"):
            plan = str(payload["plan"]).lower()
        if payload.get("features"):
            features = [str(x).lower() for x in payload["features"]]

    deadline = _validation_deadline(row)
    if deadline is not None and now > deadline:
        return SelfHostedEntitlements(
            plan="free",
            features=[],
            locked=True,
            lock_reason="LICENSE_REVALIDATION_REQUIRED",
            validation_cached_until=row.validation_cached_until,
        )

    return SelfHostedEntitlements(
        plan=plan,
        features=features,
        locked=False,
        lock_reason=None,
        validation_cached_until=row.validation_cached_until,
    )
