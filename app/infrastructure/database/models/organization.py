from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.base import Base, TimestampMixin, UUIDMixin, utcnow

if TYPE_CHECKING:
    from app.infrastructure.database.models.agent_conversation import AgentConversation
    from app.infrastructure.database.models.connection import Connection
    from app.infrastructure.database.models.pipeline_run import PipelineRun
    from app.infrastructure.database.models.recommendation import Recommendation
    from app.infrastructure.database.models.schema_mapping import SchemaMapping
    from app.infrastructure.database.models.user import User


class Organization(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    industry: Mapped[str | None] = mapped_column(String(100))
    business_context: Mapped[str | None] = mapped_column(Text)
    entity_label: Mapped[str | None] = mapped_column(String(100))
    goal_label: Mapped[str | None] = mapped_column(String(255))
    onboarding_done: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    slug: Mapped[str | None] = mapped_column(Text, unique=True)
    plan: Mapped[str] = mapped_column(Text, default="free", server_default="free")
    deployment_mode: Mapped[str] = mapped_column(Text, default="cloud", server_default="cloud")
    timezone: Mapped[str] = mapped_column(Text, default="UTC", server_default="UTC")
    logo_url: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), onupdate=utcnow
    )

    users: Mapped[list[User]] = relationship("User", back_populates="organization", lazy="raise")
    connections: Mapped[list[Connection]] = relationship("Connection", back_populates="organization", lazy="raise")
    schema_mappings: Mapped[list[SchemaMapping]] = relationship(
        "SchemaMapping", back_populates="organization", lazy="raise"
    )
    recommendations: Mapped[list[Recommendation]] = relationship(
        "Recommendation", back_populates="organization", lazy="raise"
    )
    agent_conversations: Mapped[list[AgentConversation]] = relationship(
        "AgentConversation", back_populates="organization", lazy="raise"
    )
    pipeline_runs: Mapped[list[PipelineRun]] = relationship(
        "PipelineRun", back_populates="organization", lazy="raise"
    )

    def __repr__(self) -> str:
        return f"<Organization {self.id} {self.name}>"
