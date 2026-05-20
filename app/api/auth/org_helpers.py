"""Organization ownership helpers."""

from __future__ import annotations

from uuid import UUID

from app.infrastructure.database.models.organization import Organization
from app.infrastructure.database.models.user import User


def is_org_owner(user: User, org: Organization | None) -> bool:
    if not org or not org.owner_user_id:
        return False
    return user.id == org.owner_user_id


def user_can_manage_org_security(user: User, org: Organization | None) -> bool:
    """Owner or admin may change org security settings (e.g. require_2fa)."""
    if is_org_owner(user, org):
        return True
    return user.role == "admin"
