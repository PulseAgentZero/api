from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.infrastructure.database.models.organization import Organization
    from app.infrastructure.database.models.schema_mapping import SchemaMapping


class Connection(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "connections"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    db_type: Mapped[str | None] = mapped_column(String(20))
    host: Mapped[str | None] = mapped_column(String(255))
    port: Mapped[int | None] = mapped_column(Integer)
    database_name: Mapped[str | None] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(255))
    encrypted_dsn: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="untested", server_default="untested")
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    organization: Mapped[Organization] = relationship("Organization", back_populates="connections")
    schema_mappings: Mapped[list[SchemaMapping]] = relationship("SchemaMapping", back_populates="connection", lazy="raise")

    def __repr__(self) -> str:
        return f"<Connection {self.id} {self.db_type}://{self.host}:{self.port}/{self.database_name}>"
