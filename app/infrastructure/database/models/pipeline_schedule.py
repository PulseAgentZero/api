from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.base import Base, TimestampMixin, UUIDMixin, utcnow

if TYPE_CHECKING:
    from app.infrastructure.database.models.organization import Organization
    from app.infrastructure.database.models.schema_mapping import SchemaMapping


class PipelineSchedule(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "pipeline_schedules"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    mapping_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("schema_mappings.id", ondelete="CASCADE")
    )
    cron_expression: Mapped[str] = mapped_column(
        Text, default="0 */6 * * *", server_default="0 */6 * * *"
    )
    timezone: Mapped[str] = mapped_column(Text, default="UTC", server_default="UTC")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), onupdate=utcnow
    )

    organization: Mapped[Organization] = relationship("Organization", lazy="raise")
    mapping: Mapped[SchemaMapping | None] = relationship("SchemaMapping", lazy="raise")
