"""Organization deletion — owner-confirmed via email code."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import bad_request, not_found
from app.infrastructure.database.models.organization import Organization
from app.infrastructure.database.models.user import User
from app.infrastructure.database.repositories.connection_repository import ConnectionRepository
from app.infrastructure.database.repositories.schema_mapping_repository import SchemaMappingRepository
from app.infrastructure.redis import mfa_tokens as redis_mfa
from app.infrastructure.redis import tokens as redis_tokens
from app.services.email_queue import queue_email
from app.services.totp_service import clear_totp_fields


async def send_org_delete_code(*, owner: User, org_name: str, org_id: UUID) -> None:
    code = f"{secrets.randbelow(1_000_000):06d}"
    await redis_mfa.set_org_delete_code(org_id=org_id, owner_id=owner.id, code=code)
    await queue_email(
        "org_delete_confirm",
        to=owner.email,
        code=code,
        org_name=org_name,
        full_name=owner.full_name or "",
    )


async def confirm_org_deletion(
    db: AsyncSession,
    *,
    org_id: UUID,
    owner_id: UUID,
    code: str,
) -> None:
    stored = await redis_mfa.get_org_delete_code(org_id=org_id, owner_id=owner_id)
    if not stored or stored.strip() != (code or "").strip():
        raise bad_request("INVALID_CODE", "Invalid or expired confirmation code")

    org = await db.get(Organization, org_id)
    if not org:
        raise not_found("Organization not found")

    now = datetime.now(timezone.utc)
    org.deleted_at = now
    org.slug = f"deleted-{str(org.id)[:8]}"[:80]

    conn_repo = ConnectionRepository(db)
    for conn in await conn_repo.list_by_org(org_id):
        if conn.deleted_at is None:
            await conn_repo.soft_delete(conn.id)

    map_repo = SchemaMappingRepository(db)
    for mapping in await map_repo.list_by_org(org_id):
        if mapping.is_active:
            await map_repo.update(mapping.id, is_active=False)

    result = await db.execute(select(User).where(User.org_id == org_id))
    for user in result.scalars().all():
        user.is_active = False
        clear_totp_fields(user)
        await redis_tokens.revoke_all_refresh_tokens_for_user(user.id)
        if user.id != owner_id:
            user.email = f"deleted+{user.id}@deleted.local"

    await redis_mfa.delete_org_delete_code(org_id=org_id, owner_id=owner_id)
    await db.flush()
