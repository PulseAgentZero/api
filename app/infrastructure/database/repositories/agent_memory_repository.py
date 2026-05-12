import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
            )
            self.db.add(existing)
        else:
            existing.fingerprint = fingerprint
            existing.data = data
        await self.db.flush()
        return existing
