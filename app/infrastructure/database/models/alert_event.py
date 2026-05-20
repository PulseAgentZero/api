from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.base import Base, UUIDMixin, utcnow

if TYPE_CHECKING:
    from app.infrastructure.database.models.alert_rule import AlertRule
    from app.infrastructure.database.models.organization import Organization
    from app.infrastructure.database.models.pipeline_run import PipelineRun


class AlertEvent(Base, UUIDMixin):
    __tablename__ = "alert_events"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    rule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("alert_rules.id", ondelete="CASCADE"), nullable=False
    )
    pipeline_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pipeline_runs.id", ondelete="SET NULL")
    )
    metric: Mapped[str] = mapped_column(Text, nullable=False)
    metric_value: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    threshold: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    affected_entity_ids: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    affected_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        nullable=False,
    )

    organization: Mapped[Organization] = relationship("Organization", lazy="raise")
    rule: Mapped[AlertRule] = relationship("AlertRule", back_populates="events", lazy="raise")
    pipeline_run: Mapped[PipelineRun | None] = relationship("PipelineRun", lazy="raise")
