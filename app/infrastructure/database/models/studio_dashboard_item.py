from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.infrastructure.database.models.organization import Organization
    from app.infrastructure.database.models.studio_dashboard import StudioDashboard
    from app.infrastructure.database.models.studio_visualization import StudioVisualization


class StudioDashboardItem(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "studio_dashboard_items"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dashboard_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("studio_dashboards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    visualization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("studio_visualizations.id", ondelete="SET NULL"),
        nullable=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    panel_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="visualization", server_default="visualization"
    )
    content: Mapped[str | None] = mapped_column(Text, nullable=True)

    organization: Mapped[Organization] = relationship("Organization", lazy="raise")
    dashboard: Mapped[StudioDashboard] = relationship(
        "StudioDashboard", back_populates="items", lazy="raise"
    )
    visualization: Mapped[StudioVisualization | None] = relationship(
        "StudioVisualization", lazy="raise"
    )

    def __repr__(self) -> str:
        return f"<StudioDashboardItem {self.id} dashboard={self.dashboard_id} pos={self.position}>"
