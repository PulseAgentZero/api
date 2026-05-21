from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base, TimestampMixin, UUIDMixin, utcnow


class SchedulerHeartbeat(Base, UUIDMixin, TimestampMixin):
    """One row per scheduler kind (e.g. "pipeline"). Updated by the scheduler
    on every tick so the API can surface freshness/health to the UI.
    """

    __tablename__ = "scheduler_heartbeats"

    kind: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )
    process_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    host: Mapped[str | None] = mapped_column(Text, nullable=True)
    scheduled_runs_total: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )
