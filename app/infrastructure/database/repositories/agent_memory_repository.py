import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.base import touch_updated_at
from app.infrastructure.database.models.agent_memory import AgentMemory


def compute_fingerprint(payload: Any) -> str:
    """Stable sha256 digest over a JSON-serializable payload."""
    serialized = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class AgentMemoryRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, org_id: UUID, agent_name: str) -> AgentMemory | None:
        stmt = (
            select(AgentMemory)
            .where(AgentMemory.org_id == org_id)
            .where(AgentMemory.agent_name == agent_name)
            .where(AgentMemory.scope == "org")
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert(
        self,
        org_id: UUID,
        agent_name: str,
        *,
        fingerprint: str,
        data: dict,
    ) -> AgentMemory:
        existing = await self.get(org_id, agent_name)
        if existing is None:
            existing = AgentMemory(
                org_id=org_id,
                agent_name=agent_name,
                fingerprint=fingerprint,
                data=data,
                scope="org",
            )
            self.db.add(existing)
        else:
            existing.fingerprint = fingerprint
            existing.data = data
            touch_updated_at(existing)
        await self.db.flush()
        return existing

    async def get_scoped(
        self,
        org_id: UUID,
        scope: str,
        scope_id: UUID | None,
        agent_name: str,
        *,
        key: str | None = None,
    ) -> AgentMemory | None:
        """Fetch a memory record at a non-org scope (e.g. user, conversation)."""
        stmt = (
            select(AgentMemory)
            .where(AgentMemory.org_id == org_id)
            .where(AgentMemory.scope == scope)
            .where(AgentMemory.agent_name == agent_name)
        )
        if scope_id is not None:
            stmt = stmt.where(AgentMemory.scope_id == scope_id)
        if key is not None:
            stmt = stmt.where(AgentMemory.key == key)
        stmt = stmt.limit(1)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert_scoped(
        self,
        org_id: UUID,
        scope: str,
        scope_id: UUID | None,
        agent_name: str,
        *,
        data: dict,
        key: str | None = None,
        expires_at: datetime | None = None,
    ) -> AgentMemory:
        """Upsert a scoped memory record. Use scope='user' / 'conversation' for chat memory."""
        existing = await self.get_scoped(org_id, scope, scope_id, agent_name, key=key)
        fingerprint = compute_fingerprint(data)
        if existing is None:
            existing = AgentMemory(
                org_id=org_id,
                agent_name=agent_name,
                fingerprint=fingerprint,
                data=data,
                scope=scope,
                scope_id=scope_id,
                key=key,
                expires_at=expires_at,
            )
            self.db.add(existing)
        else:
            existing.fingerprint = fingerprint
            existing.data = data
            if expires_at is not None:
                existing.expires_at = expires_at
            touch_updated_at(existing)
        await self.db.flush()
        return existing
