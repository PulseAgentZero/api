"""LDAP / Active Directory user sync for self-hosted (license: ldap_sync)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.crypto import decrypt_secret
from app.infrastructure.database.models.ldap_configuration import LdapConfiguration
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.user_repository import UserRepository

logger = logging.getLogger(__name__)


def _import_ldap3():
    try:
        from ldap3 import ALL, Connection, Server  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "ldap3 is not installed. Install it (pip install ldap3) to use LDAP sync."
        ) from exc
    return ALL, Connection, Server


def _map_role(groups: list[str], role_mapping: dict[str, Any], default_role: str) -> str:
    for group_dn, role in (role_mapping or {}).items():
        if group_dn in groups:
            return str(role)
    return default_role


@dataclass
class _LdapSnapshot:
    """Plain-data copy of LdapConfiguration safe to pass to a worker thread."""

    server_url: str
    bind_dn: str
    bind_password: str
    user_search_base: str
    user_search_filter: str
    email_attr: str
    name_attr: str
    group_attr: str | None


def _snapshot(cfg: LdapConfiguration) -> _LdapSnapshot:
    return _LdapSnapshot(
        server_url=cfg.server_url,
        bind_dn=cfg.bind_dn,
        bind_password=decrypt_secret(cfg.bind_password_encrypted),
        user_search_base=cfg.user_search_base,
        user_search_filter=cfg.user_search_filter,
        email_attr=cfg.email_attr,
        name_attr=cfg.name_attr,
        group_attr=cfg.group_attr,
    )


def _sync_ldap_blocking(snap: _LdapSnapshot) -> dict[str, Any]:
    ALL, Connection, Server = _import_ldap3()
    server = Server(snap.server_url, get_info=ALL)
    summary: dict[str, Any] = {
        "created": 0,
        "updated": 0,
        "deactivated": 0,
        "errors": [],
    }

    with Connection(server, snap.bind_dn, snap.bind_password, auto_bind=True) as conn:
        conn.search(
            snap.user_search_base,
            snap.user_search_filter,
            attributes=[snap.email_attr, snap.name_attr]
            + ([snap.group_attr] if snap.group_attr else []),
        )
        entries: list[dict[str, Any]] = []
        for entry in conn.entries:
            email_raw = entry[snap.email_attr].value if snap.email_attr in entry else None
            if not email_raw:
                continue
            email = str(email_raw).strip().lower()
            if not email or "@" not in email:
                continue
            name = ""
            if snap.name_attr in entry and entry[snap.name_attr].value:
                name = str(entry[snap.name_attr].value)
            groups: list[str] = []
            if snap.group_attr and snap.group_attr in entry and entry[snap.group_attr].value:
                val = entry[snap.group_attr].value
                groups = list(val) if isinstance(val, (list, tuple)) else [str(val)]
            entries.append({"email": email, "name": name, "groups": groups})

    summary["found"] = len(entries)
    summary["entries"] = entries
    return summary


async def sync_org_ldap(db: AsyncSession, org_id: UUID) -> dict[str, Any]:
    cfg = (
        await db.execute(select(LdapConfiguration).where(LdapConfiguration.org_id == org_id))
    ).scalar_one_or_none()
    if cfg is None or not cfg.is_active:
        return {"status": "skipped", "reason": "ldap_not_configured"}

    try:
        snap = _snapshot(cfg)
        raw = await asyncio.to_thread(_sync_ldap_blocking, snap)
    except Exception as e:
        cfg.last_sync_at = datetime.now(timezone.utc)
        cfg.last_sync_status = "error"
        cfg.last_sync_summary = {"error": str(e)[:2000]}
        await db.flush()
        return {"status": "error", "error": str(e)}

    repo = UserRepository(db)
    created = updated = deactivated = 0
    errors: list[str] = []

    for ent in raw.get("entries") or []:
        email = ent["email"]
        role = _map_role(ent.get("groups") or [], dict(cfg.role_mapping or {}), cfg.default_role)
        user = await repo.get_by_email(email)
        if user and user.org_id != org_id:
            errors.append(f"{email}: belongs to another org")
            continue
        if user is None:
            user = await repo.create(
                org_id=org_id,
                email=email,
                password_hash=None,
                role=role,
            )
            user.full_name = ent.get("name") or ""
            user.is_verified = True
            user.auth_provider = "ldap"
            user.auth_provider_id = email
            created += 1
        else:
            user.role = role
            user.is_active = True
            user.auth_provider = "ldap"
            user.auth_provider_id = email
            if ent.get("name"):
                user.full_name = ent["name"]
            updated += 1

    ldap_users = {e["email"] for e in (raw.get("entries") or [])}
    all_org = (
        await db.execute(select(User).where(User.org_id == org_id, User.auth_provider == "ldap"))
    ).scalars().all()
    for u in all_org:
        if u.email.lower() not in ldap_users and u.is_active:
            u.is_active = False
            deactivated += 1

    summary = {
        "found": raw.get("found", 0),
        "created": created,
        "updated": updated,
        "deactivated": deactivated,
        "errors": errors,
    }
    cfg.last_sync_at = datetime.now(timezone.utc)
    cfg.last_sync_status = "ok"
    cfg.last_sync_summary = summary
    await db.flush()
    return {"status": "ok", **summary}
