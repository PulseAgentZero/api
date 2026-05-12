from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models.agent_conversation import AgentConversation


class AgentConversationRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, conversation_id: UUID) -> AgentConversation | None:
        return await self.db.get(AgentConversation, conversation_id)

    async def list_by_org(self, org_id: UUID) -> list[AgentConversation]:
        stmt = (
            select(AgentConversation)
            .where(AgentConversation.org_id == org_id)
            .order_by(AgentConversation.updated_at.desc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def list_by_user(self, org_id: UUID, user_id: UUID) -> list[AgentConversation]:
        stmt = (
            select(AgentConversation)
            .where(
                AgentConversation.org_id == org_id,
                AgentConversation.user_id == user_id,
            )
            .order_by(AgentConversation.updated_at.desc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def create(self, org_id: UUID, user_id: UUID | None = None) -> AgentConversation:
        conv = AgentConversation(
            org_id=org_id,
            user_id=user_id,
            messages={"messages": []},
        )
        self.db.add(conv)
        return conv

    async def append_messages(
        self, conversation_id: UUID, *messages: dict
    ) -> AgentConversation | None:
        conv = await self.get_by_id(conversation_id)
        if not conv:
            return None
        current = {"messages": list((conv.messages or {}).get("messages", []))}
        current["messages"].extend(messages)
        conv.messages = current
        return conv

    async def delete(self, conversation_id: UUID) -> bool:
        conv = await self.get_by_id(conversation_id)
        if not conv:
            return False
        await self.db.delete(conv)
        return True
