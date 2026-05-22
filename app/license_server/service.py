"""License issuance and validation business logic."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import jwt
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.infrastructure.database.models.license_activation import LicenseActivation
from app.infrastructure.database.models.license_issuance import LicenseIssuance
from app.license_server.crypto import decode_license_jwt, issue_license_jwt
from app.license_server.settings import DEFAULT_PLAN, DEFAULT_SELF_HOSTED_FEATURES

logger = logging.getLogger(__name__)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


async def purchase_license(
    db: AsyncSession,
    *,
    payment_reference: str,
    email: str,
    purchaser_org_id: str | None,
    product: str = "self_hosted",
) -> dict:
    """Idempotent: same payment_reference returns the existing key."""
    ref = payment_reference.strip()
    if not ref:
        raise ValueError("payment_reference is required")

    existing = await db.execute(
        select(LicenseIssuance).where(LicenseIssuance.payment_reference == ref)
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        return {
            "license_key": row.license_key,
            "expires_at": _iso(row.expires_at),
            "plan": row.plan,
            "features": list(row.features or []),
            "seat_limit": row.seat_limit,
        }

    license_key, expires_at, jti = issue_license_jwt(
        plan=DEFAULT_PLAN,
        features=list(DEFAULT_SELF_HOSTED_FEATURES),
    )
    row = LicenseIssuance(
        jti=jti,
        payment_reference=ref,
        email=email.strip().lower(),
        purchaser_org_id=purchaser_org_id,
        product=product,
        plan=DEFAULT_PLAN,
        features=list(DEFAULT_SELF_HOSTED_FEATURES),
        seat_limit=None,
        expires_at=expires_at,
        license_key=license_key,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {
        "license_key": row.license_key,
        "expires_at": _iso(row.expires_at),
        "plan": row.plan,
        "features": list(row.features or []),
        "seat_limit": row.seat_limit,
    }


async def validate_license(
    db: AsyncSession,
    *,
    license_key: str,
    org_id: str,
) -> dict:
    org_id = str(org_id).strip()
    key = (license_key or "").strip()
    if not org_id or not key:
        return {"valid": False, "reason": "Missing license_key or org_id", "code": "INVALID_REQUEST"}

    try:
        payload = decode_license_jwt(key)
    except jwt.PyJWTError as exc:
        logger.info("JWT decode failed: %s", exc)
        return {"valid": False, "reason": "Invalid license signature", "code": "INVALID_LICENSE_SIGNATURE"}

    jti = str(payload.get("jti") or "")
    if not jti:
        return {"valid": False, "reason": "Invalid license token", "code": "INVALID_LICENSE"}

    now = datetime.now(timezone.utc)
    exp = payload.get("exp")
    if exp is not None:
        try:
            if datetime.fromtimestamp(int(float(exp)), tz=timezone.utc) < now:
                return {"valid": False, "reason": "License expired", "code": "LICENSE_EXPIRED"}
        except (TypeError, ValueError):
            return {"valid": False, "reason": "Invalid expiry", "code": "LICENSE_EXPIRED"}

    result = await db.execute(
        select(LicenseIssuance)
        .where(LicenseIssuance.jti == jti)
        .options(selectinload(LicenseIssuance.activation))
    )
    issuance = result.scalar_one_or_none()
    if issuance is None:
        return {"valid": False, "reason": "Unknown license key", "code": "UNKNOWN_LICENSE"}

    if issuance.revoked_at is not None:
        return {"valid": False, "reason": "License revoked", "code": "LICENSE_REVOKED"}

    if issuance.expires_at and issuance.expires_at < now:
        return {"valid": False, "reason": "License expired", "code": "LICENSE_EXPIRED"}

    activation = issuance.activation
    if activation is None:
        activation = LicenseActivation(
            issuance_id=issuance.id,
            bound_org_id=org_id,
            first_activated_at=now,
            last_validated_at=now,
        )
        db.add(activation)
        try:
            await db.commit()
        except IntegrityError:
            # Another concurrent /validate request won the race and bound the
            # license. Re-load to determine whether it bound to our org_id or
            # a different one.
            await db.rollback()
            result = await db.execute(
                select(LicenseActivation).where(LicenseActivation.issuance_id == issuance.id)
            )
            activation = result.scalar_one_or_none()
            if activation is None:
                # Shouldn't happen — log and refuse the request so the caller
                # can retry safely.
                logger.error(
                    "License activation race for jti=%s left no row; rejecting", jti
                )
                return {
                    "valid": False,
                    "reason": "Could not activate license, please retry",
                    "code": "ACTIVATION_RACE",
                }
            if activation.bound_org_id != org_id:
                return {
                    "valid": False,
                    "reason": "This license is already activated on another organization",
                    "code": "LICENSE_ALREADY_ACTIVATED",
                }
            activation.last_validated_at = now
            await db.commit()
    elif activation.bound_org_id != org_id:
        return {
            "valid": False,
            "reason": "This license is already activated on another organization",
            "code": "LICENSE_ALREADY_ACTIVATED",
        }
    else:
        activation.last_validated_at = now
        await db.commit()

    plan = str(payload.get("plan") or issuance.plan or DEFAULT_PLAN)
    features = payload.get("features") or issuance.features or list(DEFAULT_SELF_HOSTED_FEATURES)
    seat_limit = payload.get("seat_limit", issuance.seat_limit)

    limits = payload.get("limits")
    if not isinstance(limits, dict):
        from app.license_server.settings import DEFAULT_LICENSE_LIMITS

        limits = dict(DEFAULT_LICENSE_LIMITS)

    return {
        "valid": True,
        "plan": plan,
        "features": list(features),
        "limits": limits,
        "seat_limit": seat_limit,
        "expires_at": _iso(issuance.expires_at),
    }
