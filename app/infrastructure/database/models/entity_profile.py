from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.infrastructure.database.models.organization import Organization
    from app.infrastructure.database.models.pipeline_run import PipelineRun
    from app.infrastructure.database.models.schema_mapping import SchemaMapping


class EntityProfile(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "entity_profiles"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    pipeline_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pipeline_runs.id", ondelete="SET NULL")
    )
    mapping_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("schema_mappings.id", ondelete="CASCADE"), nullable=False
    )
    entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    entity_name: Mapped[str | None] = mapped_column(Text)
    segment: Mapped[str | None] = mapped_column(Text)
    profile_data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    risk_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    risk_tier: Mapped[str | None] = mapped_column(Text)
    risk_narrative: Mapped[str | None] = mapped_column(Text)
    is_latest: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    organization: Mapped[Organization] = relationship("Organization", lazy="raise")
    pipeline_run: Mapped[PipelineRun | None] = relationship("PipelineRun", lazy="raise")
    mapping: Mapped[SchemaMapping] = relationship("SchemaMapping", lazy="raise")
