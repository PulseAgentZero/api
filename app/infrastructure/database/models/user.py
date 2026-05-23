from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.base import Base, TimestampMixin, UUIDMixin, utcnow

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
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(50), default="analyst", server_default="analyst")

    full_name: Mapped[str] = mapped_column(Text, default="", server_default="")
    profile_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    auth_provider: Mapped[str] = mapped_column(Text, default="email", server_default="email")
    auth_provider_id: Mapped[str | None] = mapped_column(Text)
    sso_provider: Mapped[str | None] = mapped_column(Text)
    sso_subject: Mapped[str | None] = mapped_column(Text)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    totp_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    totp_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    totp_recovery_codes: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), onupdate=utcnow
    )

    organization: Mapped[Organization] = relationship("Organization", back_populates="users")
    actioned_recommendations: Mapped[list[Recommendation]] = relationship(
        "Recommendation", back_populates="actioned_by_user", lazy="raise",
    )
    agent_conversations: Mapped[list[AgentConversation]] = relationship(
        "AgentConversation", back_populates="user", lazy="raise",
    )

    def __repr__(self) -> str:
        return f"<User {self.id} {self.email}>"
