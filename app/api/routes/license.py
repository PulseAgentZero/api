"""Self-hosted license activation (BACKEND_ROUTES §16 + LICENSE_SYSTEM.md)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.role_deps import require_role
from app.api.errors import bad_request, not_found
from app.config.settings import settings
from app.infrastructure.database.models.license_key import LicenseKey
from app.infrastructure.database.models.organization import Organization
from app.infrastructure.database.models.user import User
from app.infrastructure.database.session import get_db
from app.infrastructure.license.jwt_verify import decode_license_jwt_payload
from app.services.license_remote import post_validate_license
from app.services.self_hosted_license import resolve_self_hosted_entitlements

router = APIRouter(prefix="/license", tags=["License"])


async def _active_seat_count(db: AsyncSession, org_id: UUID) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.org_id == org_id, User.is_active.is_(True))
        )
        or 0
    )


def _cloud_404() -> None:
    if settings.DEPLOYMENT_MODE != "self_hosted":
        raise not_found("Not found")


@router.get("")
async def get_license(
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _cloud_404()
    r = await db.execute(select(LicenseKey).where(LicenseKey.org_id == current_user.org_id))
    row = r.scalar_one_or_none()
    ent = await resolve_self_hosted_entitlements(db, current_user.org_id)
    seat_used = await _active_seat_count(db, current_user.org_id)
    if row is None:
        return {
            "plan": "free",
            "features": [],
            "is_valid": False,
            "locked": False,
            "lock_reason": None,
            "validation_cached_until": None,
            "seat_used": seat_used,
        }
    return {
        "plan": row.plan,
        "features": list(row.features or []),
        "effective_plan": ent.plan,
        "effective_features": ent.features,
        "seat_limit": row.seat_limit,
        "seat_used": seat_used,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "last_validated_at": row.last_validated_at.isoformat() if row.last_validated_at else None,
        "validation_cached_until": row.validation_cached_until.isoformat()
        if row.validation_cached_until
        else None,
        "is_active": row.is_active,
        "is_valid": row.is_active and not ent.locked,
        "locked": ent.locked,
        "lock_reason": ent.lock_reason,
    }


class ActivateBody(BaseModel):
    license_key: str


def _apply_server_payload(
    row: LicenseKey,
    license_key: str,
    data: dict,
    *,
    now: datetime,
) -> None:
    plan = data.get("plan", "pro")
    features = data.get("features", [])
    seat_limit = data.get("seat_limit")
    expires_at = data.get("expires_at")
    row.license_key = license_key
    row.plan = str(plan or "pro")
    row.features = list(features or [])
    row.seat_limit = seat_limit
    if expires_at:
        try:
            row.expires_at = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except ValueError:
            row.expires_at = None
    else:
        row.expires_at = None
    row.last_validated_at = now
    row.validation_cached_until = now + timedelta(days=settings.LICENSE_OFFLINE_GRACE_DAYS)
    row.is_active = bool(data.get("valid", True))


@router.post("/activate")
async def activate_license(
    body: ActivateBody,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _cloud_404()
    if settings.PULSE_LICENSE_PUBLIC_KEY:
        try:
            decode_license_jwt_payload(body.license_key)
        except (jwt.PyJWTError, ValueError):
            raise bad_request("INVALID_LICENSE_SIGNATURE", "License key signature could not be verified")

    code, data, err = await post_validate_license(body.license_key, current_user.org_id)
    if code == 0:
        raise bad_request("LICENSE_SERVER_UNREACHABLE", err or "Cannot reach Pulse license server")
    if code >= 400 or not data:
        msg = "Invalid license key"
        if isinstance(data, dict):
            msg = (data.get("message") or data.get("reason") or data.get("detail") or msg)
        elif err:
            msg = err
        raise bad_request("BAD_REQUEST", str(msg))
    if data.get("valid") is False:
        raise bad_request("INVALID_LICENSE", str(data.get("reason") or "License is not valid"))

    plan = data.get("plan", "pro")
    features = data.get("features", [])
    seat_limit = data.get("seat_limit")
    expires_at = data.get("expires_at")
    now = datetime.now(timezone.utc)

    r = await db.execute(select(LicenseKey).where(LicenseKey.org_id == current_user.org_id))
    row = r.scalar_one_or_none()
    if row:
        _apply_server_payload(row, body.license_key, data, now=now)
    else:
        exp_dt = None
        if expires_at:
            try:
                exp_dt = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            except ValueError:
                exp_dt = None
        row = LicenseKey(
            org_id=current_user.org_id,
            license_key=body.license_key,
            plan=str(plan or "pro"),
            features=list(features or []),
            seat_limit=seat_limit,
            expires_at=exp_dt,
            last_validated_at=now,
            validation_cached_until=now + timedelta(days=settings.LICENSE_OFFLINE_GRACE_DAYS),
            is_active=True,
        )
        db.add(row)

    org = await db.get(Organization, current_user.org_id)
    if org:
        org.plan = str(plan or "pro")
    from app.infrastructure.audit import log_audit

    await log_audit(
        db,
        org_id=current_user.org_id,
        user_id=current_user.id,
        action="license.activated",
        resource="license_key",
        resource_id=row.id,
        metadata={"plan": str(plan or "pro")},
    )
    await db.commit()
    return {
        "plan": plan,
        "features": list(features or []),
        "seat_limit": seat_limit,
        "expires_at": expires_at,
        "validation_cached_until": row.validation_cached_until.isoformat()
        if row.validation_cached_until
        else None,
        "message": "License activated successfully",
    }


@router.post("/refresh")
async def refresh_license(
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _cloud_404()
    r = await db.execute(select(LicenseKey).where(LicenseKey.org_id == current_user.org_id))
    row = r.scalar_one_or_none()
    if row is None:
        raise bad_request("BAD_REQUEST", "No license to refresh")
    return await activate_license(ActivateBody(license_key=row.license_key), current_user, db)
