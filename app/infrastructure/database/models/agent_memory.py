from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base, TimestampMixin, UUIDMixin, utcnow

if TYPE_CHECKING:  # noqa: F401
    pass


class AgentMemory(Base, UUIDMixin, TimestampMixin):
    """Cached output for an autonomous agent keyed by (org, agent_name).

    The fingerprint is a hash of the relevant inputs (e.g. raw_schema) so
    callers can skip re-running the agent when nothing material has changed.
    """

    __tablename__ = "agent_memory"
    __table_args__ = (
        UniqueConstraint("org_id", "agent_name", name="uq_agent_memory_org_agent"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_name: Mapped[str] = mapped_column(String(100), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    data: Mapped[dict | None] = mapped_column(JSONB)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        onupdate=utcnow,
    )

    def __repr__(self) -> str:
        return f"<AgentMemory org={self.org_id} agent={self.agent_name}>"
