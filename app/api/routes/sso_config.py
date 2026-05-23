"""Self-hosted SSO configuration (license: sso)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.role_deps import require_role
from app.api.dependencies.plan_gate import require_feature
from app.api.errors import bad_request, not_found
from app.config.settings import settings
from app.infrastructure.crypto import decrypt_secret, encrypt_secret
from app.infrastructure.database.models.sso_configuration import SsoConfiguration
from app.infrastructure.database.models.user import User
from app.infrastructure.database.session import get_db

router = APIRouter(prefix="/sso", tags=["SSO"])


def _self_hosted_only() -> None:
    if settings.DEPLOYMENT_MODE != "self_hosted":
        raise not_found("Not found")


def _redact(row: SsoConfiguration) -> dict:
    return {
        "id": str(row.id),
        "org_id": str(row.org_id),
        "provider": row.provider,
        "is_active": row.is_active,
        "client_id": row.client_id,
        "client_secret_set": bool(row.client_secret_encrypted),
        "discovery_url": row.discovery_url,
        "scopes": row.scopes,
        "email_claim": row.email_claim,
        "name_claim": row.name_claim,
        "entity_id": row.entity_id,
        "metadata_url": row.metadata_url,
        "metadata_xml_set": bool(row.metadata_xml),
        "acs_url_path": row.acs_url_path,
        "name_id_format": row.name_id_format,
        "default_role": row.default_role,
        "auto_provision_users": row.auto_provision_users,
        "allowed_email_domains": list(row.allowed_email_domains or []),
        "updated_at": row.updated_at.isoformat(),
    }


class SsoConfigBody(BaseModel):
    provider: str
    is_active: bool = False
    client_id: str | None = None
    client_secret: str | None = None
    discovery_url: str | None = None
    scopes: str | None = "openid email profile"
    email_claim: str = "email"
    name_claim: str = "name"
    entity_id: str | None = None
    metadata_xml: str | None = None
    metadata_url: str | None = None
    acs_url_path: str | None = None
    name_id_format: str | None = None
    default_role: str = "viewer"
    auto_provision_users: bool = True
    allowed_email_domains: list[str] = Field(default_factory=list)


@router.get("/config")
async def get_sso_config(
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _self_hosted_only()
    await require_feature(db, current_user.org_id, "sso")
    row = (
        await db.execute(
            select(SsoConfiguration).where(SsoConfiguration.org_id == current_user.org_id)
        )
    ).scalar_one_or_none()
    if row is None:
        return {"config": None}
    return {"config": _redact(row)}


@router.put("/config")
async def upsert_sso_config(
    body: SsoConfigBody,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _self_hosted_only()
    await require_feature(db, current_user.org_id, "sso")
    if body.provider not in ("oidc", "saml"):
        raise bad_request("BAD_REQUEST", "provider must be oidc or saml")
    if body.provider == "oidc" and not body.discovery_url:
        raise bad_request("BAD_REQUEST", "OIDC requires discovery_url")
    if body.provider == "saml" and not (body.metadata_xml or body.metadata_url):
        raise bad_request("BAD_REQUEST", "SAML requires metadata_xml or metadata_url")

    row = (
        await db.execute(
            select(SsoConfiguration).where(SsoConfiguration.org_id == current_user.org_id)
        )
    ).scalar_one_or_none()
    if row is None:
        row = SsoConfiguration(org_id=current_user.org_id, provider=body.provider)
        db.add(row)

    row.provider = body.provider
    row.is_active = body.is_active
    row.client_id = body.client_id
    if body.client_secret:
        row.client_secret_encrypted = encrypt_secret(body.client_secret)
    row.discovery_url = body.discovery_url
    row.scopes = body.scopes
    row.email_claim = body.email_claim
    row.name_claim = body.name_claim
    row.entity_id = body.entity_id
    if body.metadata_xml:
        row.metadata_xml = body.metadata_xml
    row.metadata_url = body.metadata_url
    row.acs_url_path = body.acs_url_path
    row.name_id_format = body.name_id_format
    row.default_role = body.default_role
    row.auto_provision_users = body.auto_provision_users
    row.allowed_email_domains = [d.strip().lower() for d in body.allowed_email_domains if d.strip()]

    await db.commit()
    await db.refresh(row)
    return {"config": _redact(row)}


@router.delete("/config")
async def delete_sso_config(
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _self_hosted_only()
    await require_feature(db, current_user.org_id, "sso")
    row = (
        await db.execute(
            select(SsoConfiguration).where(SsoConfiguration.org_id == current_user.org_id)
        )
    ).scalar_one_or_none()
    if row:
        await db.delete(row)
        await db.commit()
    return {"message": "SSO configuration removed"}
