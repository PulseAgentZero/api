"""Self-hosted LDAP sync (license: ldap_sync)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.role_deps import require_role
from app.api.dependencies.plan_gate import require_feature
from app.api.errors import bad_request, not_found
from app.config.settings import settings
from app.infrastructure.crypto import encrypt_secret
from app.infrastructure.database.models.ldap_configuration import LdapConfiguration
from app.infrastructure.database.models.user import User
from app.infrastructure.database.session import get_db
from app.services.ldap_sync import sync_org_ldap

router = APIRouter(prefix="/ldap", tags=["LDAP"])


def _self_hosted_only() -> None:
    if settings.DEPLOYMENT_MODE != "self_hosted":
        raise not_found("Not found")


def _redact(row: LdapConfiguration) -> dict:
    return {
        "id": str(row.id),
        "is_active": row.is_active,
        "server_url": row.server_url,
        "bind_dn": row.bind_dn,
        "bind_password_set": bool(row.bind_password_encrypted),
        "user_search_base": row.user_search_base,
        "user_search_filter": row.user_search_filter,
        "email_attr": row.email_attr,
        "name_attr": row.name_attr,
        "group_attr": row.group_attr,
        "default_role": row.default_role,
        "role_mapping": dict(row.role_mapping or {}),
        "sync_schedule_cron": row.sync_schedule_cron,
        "last_sync_at": row.last_sync_at.isoformat() if row.last_sync_at else None,
        "last_sync_status": row.last_sync_status,
        "last_sync_summary": row.last_sync_summary,
        "updated_at": row.updated_at.isoformat(),
    }


class LdapConfigBody(BaseModel):
    is_active: bool = False
    server_url: str
    bind_dn: str
    bind_password: str | None = None
    user_search_base: str
    user_search_filter: str = "(objectClass=person)"
    email_attr: str = "mail"
    name_attr: str = "cn"
    group_attr: str | None = None
    default_role: str = "viewer"
    role_mapping: dict[str, str] = Field(default_factory=dict)
    sync_schedule_cron: str = "0 */6 * * *"


@router.get("/config")
async def get_ldap_config(
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _self_hosted_only()
    await require_feature(db, current_user.org_id, "ldap_sync")
    row = (
        await db.execute(
            select(LdapConfiguration).where(LdapConfiguration.org_id == current_user.org_id)
        )
    ).scalar_one_or_none()
    if row is None:
        return {"config": None}
    return {"config": _redact(row)}


@router.put("/config")
async def upsert_ldap_config(
    body: LdapConfigBody,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _self_hosted_only()
    await require_feature(db, current_user.org_id, "ldap_sync")
    if not body.server_url.strip() or not body.bind_dn.strip():
        raise bad_request("BAD_REQUEST", "server_url and bind_dn are required")

    row = (
        await db.execute(
            select(LdapConfiguration).where(LdapConfiguration.org_id == current_user.org_id)
        )
    ).scalar_one_or_none()
    if row is None:
        if not body.bind_password:
            raise bad_request("BAD_REQUEST", "bind_password required on first setup")
        row = LdapConfiguration(
            org_id=current_user.org_id,
            server_url=body.server_url.strip(),
            bind_dn=body.bind_dn.strip(),
            bind_password_encrypted=encrypt_secret(body.bind_password),
            user_search_base=body.user_search_base.strip(),
        )
        db.add(row)
    else:
        row.server_url = body.server_url.strip()
        row.bind_dn = body.bind_dn.strip()
        if body.bind_password:
            row.bind_password_encrypted = encrypt_secret(body.bind_password)

    row.is_active = body.is_active
    row.user_search_base = body.user_search_base.strip()
    row.user_search_filter = body.user_search_filter
    row.email_attr = body.email_attr
    row.name_attr = body.name_attr
    row.group_attr = body.group_attr
    row.default_role = body.default_role
    row.role_mapping = dict(body.role_mapping)
    row.sync_schedule_cron = body.sync_schedule_cron

    await db.commit()
    await db.refresh(row)
    return {"config": _redact(row)}


@router.delete("/config")
async def delete_ldap_config(
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _self_hosted_only()
    await require_feature(db, current_user.org_id, "ldap_sync")
    row = (
        await db.execute(
            select(LdapConfiguration).where(LdapConfiguration.org_id == current_user.org_id)
        )
    ).scalar_one_or_none()
    if row:
        await db.delete(row)
        await db.commit()
    return {"message": "LDAP configuration removed"}


@router.post("/test-connection")
async def test_ldap_connection(
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _self_hosted_only()
    await require_feature(db, current_user.org_id, "ldap_sync")
    row = (
        await db.execute(
            select(LdapConfiguration).where(LdapConfiguration.org_id == current_user.org_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise bad_request("BAD_REQUEST", "Configure LDAP first")

    import asyncio
    from app.infrastructure.crypto import decrypt_secret

    server_url = row.server_url
    bind_dn = row.bind_dn
    try:
        bind_password = decrypt_secret(row.bind_password_encrypted)
    except Exception:
        return {"success": False, "message": "Stored bind password could not be decrypted"}

    def _check() -> tuple[bool, str]:
        try:
            from ldap3 import ALL, Connection, Server  # type: ignore
        except ImportError:
            return False, "ldap3 is not installed on the server"
        try:
            server = Server(server_url, get_info=ALL)
            with Connection(server, bind_dn, bind_password, auto_bind=True) as conn:
                return bool(conn.bound), "Connection successful"
        except Exception as exc:  # noqa: BLE001 - surface any failure to user
            return False, str(exc)[:2000]

    ok, msg = await asyncio.to_thread(_check)
    return {"success": ok, "message": msg}


@router.post("/sync-now")
async def ldap_sync_now(
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _self_hosted_only()
    await require_feature(db, current_user.org_id, "ldap_sync")
    result = await sync_org_ldap(db, current_user.org_id)
    await db.commit()
    return result
