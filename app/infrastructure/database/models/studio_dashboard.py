from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.base import Base, TimestampMixin, UUIDMixin, utcnow

if TYPE_CHECKING:
    from app.infrastructure.database.models.organization import Organization
    from app.infrastructure.database.models.studio_dashboard_item import StudioDashboardItem
    from app.infrastructure.database.models.user import User


class StudioDashboard(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "studio_dashboards"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    slug: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True, index=True)
    tags: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    dashboard_params: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    is_public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    layout: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        onupdate=utcnow,
    )

    organization: Mapped[Organization] = relationship("Organization", lazy="raise")
    created_by_user: Mapped[User | None] = relationship("User", lazy="raise")
    items: Mapped[list[StudioDashboardItem]] = relationship(
        "StudioDashboardItem",
        back_populates="dashboard",
        cascade="all, delete-orphan",
        lazy="raise",
    )

    def __repr__(self) -> str:
        return f"<StudioDashboard {self.id} name={self.name!r} public={self.is_public}>"
