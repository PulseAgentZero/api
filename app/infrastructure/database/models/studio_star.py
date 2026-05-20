from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.infrastructure.database.models.organization import Organization
    from app.infrastructure.database.models.user import User


class StudioStar(Base, UUIDMixin, TimestampMixin):
    """User-starred query or dashboard."""

    __tablename__ = "studio_stars"
    __table_args__ = (
        UniqueConstraint("user_id", "resource_type", "resource_id", name="uq_studio_star"),
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    resource_type: Mapped[str] = mapped_column(String(20), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    user: Mapped[User | None] = relationship("User", lazy="raise")
    organization: Mapped[Organization] = relationship("Organization", lazy="raise")

    def __repr__(self) -> str:
        return f"<StudioStar {self.user_id} → {self.resource_type}:{self.resource_id}>"
