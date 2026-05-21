"""Organization deletion — owner-confirmed via email code.

This is a true hard delete: the organization row and every row that
references it via ``organizations.id`` are removed from the database.
All FK relationships pointing at ``organizations.id`` are declared
``ON DELETE CASCADE`` in the schema, so a single ``DELETE`` on the org
row is enough to wipe users, connections, schema mappings, recommendations,
audit logs, pipeline runs, studio assets, billing records, license keys,
LLM key store, notifications, usage events, alert rules/events, webhook
deliveries, and so on.

Two FKs reference ``users.id`` without an ``ondelete`` rule and must be
cleaned up *before* the cascade fires, otherwise Postgres rejects the
delete:

* ``recommendations.actioned_by`` — anonymized to NULL (the row would be
  cascade-deleted anyway via ``recommendations.org_id``, but Postgres
  evaluates the FK from the user side first).
* ``agent_conversations.user_id`` — the conversations are about to be
  cascade-deleted via ``agent_conversations.org_id``; we just do it
  explicitly first to drop the user FK reference cleanly.
"""

from __future__ import annotations

import logging
import secrets
from uuid import UUID

from sqlalchemy import delete as sa_delete, select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import bad_request, not_found
from app.infrastructure.database.models.agent_conversation import AgentConversation
from app.infrastructure.database.models.organization import Organization
from app.infrastructure.database.models.recommendation import Recommendation
from app.infrastructure.database.models.user import User
from app.infrastructure.redis import mfa_tokens as redis_mfa
from app.infrastructure.redis import tokens as redis_tokens
from app.services.email_queue import queue_email

logger = logging.getLogger(__name__)


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
    """Hard-delete the organization and every row that belongs to it.

    On success the organization, its users, and every cascading record are
    gone from the database. Refresh tokens for all members are revoked in
    Redis and any uploaded logos / avatars are removed from object storage
    on a best-effort basis (failures do not roll back the database delete).
    """
    stored = await redis_mfa.get_org_delete_code(org_id=org_id, owner_id=owner_id)
    if not stored or stored.strip() != (code or "").strip():
        raise bad_request("INVALID_CODE", "Invalid or expired confirmation code")

    org = await db.get(Organization, org_id)
    if not org:
        raise not_found("Organization not found")

    # Snapshot anything we need after the row is gone.
    org_name = org.name
    org_logo_url = org.logo_url

    user_rows = (
        await db.execute(
            select(User.id, User.email, User.profile_image_url).where(User.org_id == org_id),
        )
    ).all()
    user_ids: list[UUID] = [row.id for row in user_rows]
    user_avatars: list[str] = [row.profile_image_url for row in user_rows if row.profile_image_url]

    # Break the two FKs that would otherwise block the cascade.
    await db.execute(
        sa_update(Recommendation)
        .where(Recommendation.org_id == org_id)
        .values(actioned_by=None),
    )
    await db.execute(
        sa_delete(AgentConversation).where(AgentConversation.org_id == org_id),
    )

    # The single delete that fans out to every other table via CASCADE.
    await db.delete(org)
    await db.commit()

    logger.warning(
        "Organization hard-deleted: org_id=%s name=%r owner_id=%s users=%d",
        org_id,
        org_name,
        owner_id,
        len(user_ids),
    )

    # Best-effort post-commit cleanup. Failures here are non-fatal — the DB
    # records are already gone and the user cannot recover the account.
    try:
        await redis_mfa.delete_org_delete_code(org_id=org_id, owner_id=owner_id)
    except Exception:  # pragma: no cover - cleanup
        logger.exception("Failed to clear org delete code for org %s", org_id)

    for uid in user_ids:
        try:
            await redis_tokens.revoke_all_refresh_tokens_for_user(uid)
        except Exception:  # pragma: no cover - cleanup
            logger.exception("Failed to revoke refresh tokens for user %s", uid)

    asset_urls = [u for u in (org_logo_url, *user_avatars) if u]
    if asset_urls:
        try:
            from app.infrastructure.storage.factory import get_storage_backend

            backend = get_storage_backend()
            if backend.is_configured():
                for url in asset_urls:
                    object_key = _object_key_from_url(url)
                    if not object_key:
                        continue
                    try:
                        await backend.delete(object_key)
                    except Exception:  # pragma: no cover - cleanup
                        logger.exception("Failed to delete storage object %s", object_key)
        except Exception:  # pragma: no cover - cleanup
            logger.exception("Storage backend unavailable while purging org %s", org_id)


def _object_key_from_url(url: str) -> str | None:
    """Derive an object key from a public asset URL. Mirror of users.py."""
    try:
        from urllib.parse import urlparse

        path = urlparse(url).path.lstrip("/")
        if not path:
            return None
        if path.startswith("assets/"):
            path = path[len("assets/"):]
        return path or None
    except Exception:
        return None
