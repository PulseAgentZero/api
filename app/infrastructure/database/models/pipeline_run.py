from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.base import Base, TimestampMixin, UUIDMixin, utcnow

if TYPE_CHECKING:
    from app.infrastructure.database.models.organization import Organization


class PipelineRun(Base, UUIDMixin, TimestampMixin):
    """Durable audit record for one autonomous pipeline execution.

    Stores metadata only — never client rows, profiles, or scored entities.
    """

    __tablename__ = "pipeline_runs"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Lifecycle: queued | running | succeeded | failed | skipped
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="queued", server_default="queued", index=True,
    )
    # scheduled | manual | onboarding
    trigger_source: Mapped[str] = mapped_column(
        String(20), nullable=False, default="manual", server_default="manual",
    )
    current_step: Mapped[str | None] = mapped_column(String(50))
    error: Mapped[str | None] = mapped_column(Text)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    # Aggregate counts (no client rows)
    entities_scored: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    critical_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    high_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    recommendations_generated: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # LLM / tool metrics
    total_llm_calls: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_tool_calls: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    provider_fallbacks: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # Per-step timings + provider/token breakdowns
    step_metrics: Mapped[list | None] = mapped_column(JSONB)
    # Cap/sampling/truncation notes (e.g. {"narratives_capped_at": 50})
    generation_caps: Mapped[dict | None] = mapped_column(JSONB)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        onupdate=utcnow,
    )

    organization: Mapped[Organization] = relationship(
        "Organization", back_populates="pipeline_runs"
    )

    def __repr__(self) -> str:
        return f"<PipelineRun {self.id} org={self.org_id} status={self.status}>"
