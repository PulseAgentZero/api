from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.infrastructure.database.models.connection import Connection
    from app.infrastructure.database.models.organization import Organization


class SchemaMapping(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "schema_mappings"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    entity_table: Mapped[str | None] = mapped_column(String(255))
    entity_id_col: Mapped[str | None] = mapped_column(String(255))
    entity_name_col: Mapped[str | None] = mapped_column(String(255))
    signal_columns: Mapped[dict | None] = mapped_column(JSONB)
    timestamp_col: Mapped[str | None] = mapped_column(String(255))
    risk_config: Mapped[dict | None] = mapped_column(JSONB)
    raw_schema: Mapped[dict | None] = mapped_column(JSONB)
    target_column: Mapped[str | None] = mapped_column(String(255))

    organization: Mapped[Organization] = relationship("Organization", back_populates="schema_mappings")
    connection: Mapped[Connection] = relationship("Connection", back_populates="schema_mappings")

    def __repr__(self) -> str:
        return f"<SchemaMapping {self.id} table={self.entity_table}>"
