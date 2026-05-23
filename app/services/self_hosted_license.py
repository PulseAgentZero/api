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

DEFAULT_CONCURRENT_PIPELINE_RUNS = 5


def _parse_limits(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in raw.items():
        try:
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


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
    limits: dict[str, int]
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
            limits={},
            locked=False,
            lock_reason=None,
            validation_cached_until=None,
        )

    plan = (row.plan or "free").lower()
    features = [str(x).lower() for x in (row.features or [])]
    limits: dict[str, int] = _parse_limits(getattr(row, "limits", None))

    if settings.PULSE_LICENSE_PUBLIC_KEY:
        try:
            payload = decode_license_jwt_payload(row.license_key)
        except jwt.PyJWTError as exc:
            logger.warning("License JWT verification failed for org %s: %s", org_id, exc)
            # If the license server has already validated this key recently,
            # trust the cached server payload until the normal offline grace
            # window expires. This keeps legitimate customers online even when
            # a self-hosted image was accidentally built with a stale public key.
            deadline = _validation_deadline(row)
            if deadline is not None and now <= deadline:
                return SelfHostedEntitlements(
                    plan=plan,
                    features=features,
                    limits=limits,
                    locked=False,
                    lock_reason=None,
                    validation_cached_until=row.validation_cached_until,
                )
            return SelfHostedEntitlements(
                plan="free",
                features=[],
                limits={},
                locked=True,
                lock_reason="INVALID_LICENSE_SIGNATURE",
                validation_cached_until=row.validation_cached_until,
            )
        if jwt_expired(payload, now=now):
            return SelfHostedEntitlements(
                plan="free",
                features=[],
                limits={},
                locked=True,
                lock_reason="LICENSE_EXPIRED",
                validation_cached_until=row.validation_cached_until,
            )
        if payload.get("plan"):
            plan = str(payload["plan"]).lower()
        if payload.get("features"):
            features = [str(x).lower() for x in payload["features"]]
        jwt_limits = _parse_limits(payload.get("limits"))
        if jwt_limits:
            limits = jwt_limits

    deadline = _validation_deadline(row)
    if deadline is not None and now > deadline:
        return SelfHostedEntitlements(
            plan="free",
            features=[],
            limits={},
            locked=True,
            lock_reason="LICENSE_REVALIDATION_REQUIRED",
            validation_cached_until=row.validation_cached_until,
        )

    return SelfHostedEntitlements(
        plan=plan,
        features=features,
        limits=limits,
        locked=False,
        lock_reason=None,
        validation_cached_until=row.validation_cached_until,
    )


async def get_concurrent_pipeline_limit(db: AsyncSession, org_id: UUID) -> int:
    """Max in-flight pipeline runs for org (1 without high_concurrency license)."""
    ent = await resolve_self_hosted_entitlements(db, org_id)
    if ent.locked or "high_concurrency" not in ent.features:
        return 1
    return max(1, ent.limits.get("concurrent_pipeline_runs", DEFAULT_CONCURRENT_PIPELINE_RUNS))
