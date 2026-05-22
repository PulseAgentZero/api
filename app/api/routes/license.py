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


async def _maybe_bootstrap_env_license(
    db: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
) -> tuple[LicenseKey | None, str | None]:
    """Activate ``ENTIVIA_LICENSE_KEY`` from env on first read if no license is stored.

    Returns ``(license_row, error_message)``. Both ``None`` means no env key
    was configured. A non-``None`` ``error_message`` means the env key failed
    validation (logged, surfaced to UI as ``env_provision_error``) but does
    not raise — the dashboard still loads.
    """
    env_key = (settings.ENTIVIA_LICENSE_KEY or "").strip()
    if not env_key:
        return None, None

    existing = await db.execute(select(LicenseKey).where(LicenseKey.org_id == org_id))
    if existing.scalar_one_or_none() is not None:
        return None, None

    if settings.PULSE_LICENSE_PUBLIC_KEY:
        try:
            decode_license_jwt_payload(env_key)
        except (jwt.PyJWTError, ValueError) as exc:
            msg = f"ENTIVIA_LICENSE_KEY signature invalid: {exc}"
            import logging as _log
            _log.getLogger(__name__).warning(msg)
            return None, "ENTIVIA_LICENSE_KEY signature could not be verified"

    code, data, err = await post_validate_license(env_key, org_id)
    if code == 0 or code >= 400 or not isinstance(data, dict) or data.get("valid") is False:
        reason = (data or {}).get("reason") if isinstance(data, dict) else err
        import logging as _log
        _log.getLogger(__name__).warning(
            "ENTIVIA_LICENSE_KEY env auto-activation failed for org %s: %s", org_id, reason
        )
        return None, f"ENTIVIA_LICENSE_KEY could not be activated: {reason or 'unreachable'}"

    now = datetime.now(timezone.utc)
    expires_at = data.get("expires_at")
    exp_dt: datetime | None = None
    if expires_at:
        try:
            exp_dt = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except ValueError:
            exp_dt = None
    row = LicenseKey(
        org_id=org_id,
        license_key=env_key,
        plan=str(data.get("plan") or "pro"),
        features=list(data.get("features") or []),
        limits=data.get("limits") if isinstance(data.get("limits"), dict) else {},
        seat_limit=data.get("seat_limit"),
        expires_at=exp_dt,
        last_validated_at=now,
        validation_cached_until=now + timedelta(days=settings.LICENSE_OFFLINE_GRACE_DAYS),
        is_active=True,
    )
    db.add(row)

    org = await db.get(Organization, org_id)
    if org:
        org.plan = str(data.get("plan") or "pro")

    from app.infrastructure.audit import log_audit

    await log_audit(
        db,
        org_id=org_id,
        user_id=user_id,
        action="license.activated",
        resource="license_key",
        resource_id=row.id,
        metadata={"plan": str(data.get("plan") or "pro"), "source": "env"},
    )
    await db.commit()
    await db.refresh(row)
    return row, None


@router.get("")
async def get_license(
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """**Self-hosted only.** Returns 404 on cloud deployments.

    Return the current license status for this org: plan, active features, seat usage,
    expiry date, and whether the instance is locked. No license → `plan: "free"`, `is_valid: false`.

    When ``ENTIVIA_LICENSE_KEY`` is set in the instance environment and no license
    is stored locally, the key is auto-activated on this call so admins do not
    need to paste it into the dashboard manually.
    """
    _cloud_404()
    r = await db.execute(select(LicenseKey).where(LicenseKey.org_id == current_user.org_id))
    row = r.scalar_one_or_none()
    env_error: str | None = None
    if row is None:
        row, env_error = await _maybe_bootstrap_env_license(
            db, org_id=current_user.org_id, user_id=current_user.id
        )

    env_provisioned = bool(
        settings.ENTIVIA_LICENSE_KEY
        and row is not None
        and row.license_key == settings.ENTIVIA_LICENSE_KEY
    )

    ent = await resolve_self_hosted_entitlements(db, current_user.org_id)
    seat_used = await _active_seat_count(db, current_user.org_id)
    if row is None:
        return {
            "plan": "free",
            "features": [],
            "limits": {},
            "effective_limits": {},
            "is_valid": False,
            "locked": False,
            "lock_reason": None,
            "validation_cached_until": None,
            "seat_used": seat_used,
            "env_provisioned": False,
            "env_provision_error": env_error,
        }
    return {
        "plan": row.plan,
        "features": list(row.features or []),
        "limits": ent.limits,
        "effective_plan": ent.plan,
        "effective_features": ent.features,
        "effective_limits": ent.limits,
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
        "env_provisioned": env_provisioned,
        "env_provision_error": None,
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
    limits = data.get("limits") if isinstance(data.get("limits"), dict) else {}
    seat_limit = data.get("seat_limit")
    expires_at = data.get("expires_at")
    row.license_key = license_key
    row.plan = str(plan or "pro")
    row.features = list(features or [])
    row.limits = limits
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
    """**Self-hosted only.** Returns 404 on cloud deployments.

    Activate or replace the license key for this self-hosted instance. Contacts the Entivia
    license server to validate the key, then stores plan, features, seat limit, and expiry
    locally. Sets a validation cache window (default 7 days) so the instance stays functional
    if the license server is temporarily unreachable. Returns 422 on invalid or expired keys.
    """
    _cloud_404()
    if settings.PULSE_LICENSE_PUBLIC_KEY:
        try:
            decode_license_jwt_payload(body.license_key)
        except (jwt.PyJWTError, ValueError):
            raise bad_request("INVALID_LICENSE_SIGNATURE", "License key signature could not be verified")

    code, data, err = await post_validate_license(body.license_key, current_user.org_id)
    if code == 0:
        raise bad_request("LICENSE_SERVER_UNREACHABLE", err or "Cannot reach Entivia license server")
    if code >= 400 or not data:
        msg = "Invalid license key"
        if isinstance(data, dict):
            msg = (data.get("message") or data.get("reason") or data.get("detail") or msg)
        elif err:
            msg = err
        raise bad_request("INVALID_LICENSE", msg)
    if data.get("valid") is False:
        reason = str(data.get("reason") or "License is not valid")
        err_code = str(data.get("code") or "INVALID_LICENSE")
        # Forward the precise upstream code so the frontend can render a tailored
        # message (e.g. "already activated", "revoked", "expired").
        raise bad_request(err_code, reason)

    plan = data.get("plan", "pro")
    features = data.get("features", [])
    limits = data.get("limits") if isinstance(data.get("limits"), dict) else {}
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
            limits=limits,
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
        "limits": limits,
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
    """**Self-hosted only.** Returns 404 on cloud deployments.

    Re-validate the stored license key against the Entivia license server and refresh the
    local cache. Call this manually if the instance was offline during the normal validation
    window and is now showing as locked.
    """
    _cloud_404()
    r = await db.execute(select(LicenseKey).where(LicenseKey.org_id == current_user.org_id))
    row = r.scalar_one_or_none()
    if row is None:
        raise bad_request("BAD_REQUEST", "No license to refresh")
    return await activate_license(ActivateBody(license_key=row.license_key), current_user, db)
