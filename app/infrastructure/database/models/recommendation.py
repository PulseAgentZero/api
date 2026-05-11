from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.infrastructure.database.models.organization import Organization
    from app.infrastructure.database.models.user import User


class Recommendation(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "recommendations"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    entity_id: Mapped[str | None] = mapped_column(String(255))
    entity_label: Mapped[str | None] = mapped_column(String(255))
    type: Mapped[str | None] = mapped_column(String(100))
    urgency: Mapped[str | None] = mapped_column(String(20))
    title: Mapped[str | None] = mapped_column(String(255))
    reasoning: Mapped[str | None] = mapped_column(Text)
    suggested_action: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(50), default="active", server_default="active")
    actioned_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
    )
    actioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    organization: Mapped[Organization] = relationship("Organization", back_populates="recommendations")
    actioned_by_user: Mapped[User | None] = relationship("User", back_populates="actioned_recommendations")

    def __repr__(self) -> str:
        return f"<Recommendation {self.id} entity={self.entity_id} urgency={self.urgency}>"
