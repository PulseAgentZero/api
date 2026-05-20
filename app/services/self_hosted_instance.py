"""Self-hosted instance constraints (single organization per deployment)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import conflict
from app.config.settings import settings
from app.infrastructure.database.repositories.organization_repository import (
    OrganizationRepository,
)

_INSTANCE_ORG_MESSAGE = (
    "This Pulse instance already has an organization. "
    "Sign in with your existing account or ask an admin for an invite."
)


async def count_organizations(db: AsyncSession) -> int:
    return await OrganizationRepository(db).count_all()


async def instance_registration_open(db: AsyncSession) -> bool:
    """True when a new organization may be created on this deployment."""
    if settings.DEPLOYMENT_MODE != "self_hosted":
        return True
    return await count_organizations(db) < 1


async def ensure_can_create_organization(db: AsyncSession) -> None:
    """Block second org creation on self-hosted instances."""
    if settings.DEPLOYMENT_MODE != "self_hosted":
        return
    if await count_organizations(db) >= 1:
        raise conflict("INSTANCE_ORG_EXISTS", _INSTANCE_ORG_MESSAGE)
