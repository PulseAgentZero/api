from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.infrastructure.database.models.agent_conversation import AgentConversation
    from app.infrastructure.database.models.organization import Organization
    from app.infrastructure.database.models.recommendation import Recommendation


class User(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "users"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(50), default="ops_manager", server_default="ops_manager")

    organization: Mapped[Organization] = relationship("Organization", back_populates="users")
    actioned_recommendations: Mapped[list[Recommendation]] = relationship(
        "Recommendation", back_populates="actioned_by_user", lazy="raise",
    )
    agent_conversations: Mapped[list[AgentConversation]] = relationship(
        "AgentConversation", back_populates="user", lazy="raise",
    )

    def __repr__(self) -> str:
        return f"<User {self.id} {self.email}>"
