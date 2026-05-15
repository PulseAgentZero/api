from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.base import Base, TimestampMixin, UUIDMixin, utcnow

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
    encrypted_dsn: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_error: Mapped[str | None] = mapped_column(Text)

    name: Mapped[str] = mapped_column(Text, default="My Connection", server_default="My Connection")
    # connector_type is the stable discriminator: postgresql, mysql, s3, bigquery, etc.
    connector_type: Mapped[str] = mapped_column(Text, default="postgresql", server_default="postgresql")

    # All connector-specific fields (host, port, db_type, sslmode, etc.) live here.
    connection_meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")

    credentials: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict, server_default="{}")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), onupdate=utcnow
    )

    organization: Mapped[Organization] = relationship("Organization", back_populates="connections")
    schema_mappings: Mapped[list[SchemaMapping]] = relationship(
        "SchemaMapping", back_populates="connection", lazy="raise"
    )

    # ── Convenience accessors that read/write connection_meta ─────────────────
    # This lets the rest of the codebase use conn.host, conn.db_type, etc.
    # without knowing about the JSONB layout, and makes future connector types
    # trivial to add (no migration needed).

    def _meta_get(self, key: str):
        return (self.connection_meta or {}).get(key)

    def _meta_set(self, key: str, value) -> None:
        meta = dict(self.connection_meta or {})
        if value is None:
            meta.pop(key, None)
        else:
            meta[key] = value
        self.connection_meta = meta

    @property
    def db_type(self) -> str | None:
        return self._meta_get("db_type")

    @db_type.setter
    def db_type(self, value: str | None) -> None:
        self._meta_set("db_type", value)

    @property
    def host(self) -> str | None:
        return self._meta_get("host")

    @host.setter
    def host(self, value: str | None) -> None:
        self._meta_set("host", value)

    @property
    def port(self) -> int | None:
        v = self._meta_get("port")
        return int(v) if v is not None else None

    @port.setter
    def port(self, value: int | None) -> None:
        self._meta_set("port", value)

    @property
    def database_name(self) -> str | None:
        return self._meta_get("database_name")

    @database_name.setter
    def database_name(self, value: str | None) -> None:
        self._meta_set("database_name", value)

    @property
    def username(self) -> str | None:
        return self._meta_get("username")

    @username.setter
    def username(self, value: str | None) -> None:
        self._meta_set("username", value)

    @property
    def sslmode(self) -> str | None:
        return self._meta_get("sslmode") or "prefer"

    @sslmode.setter
    def sslmode(self, value: str | None) -> None:
        self._meta_set("sslmode", value or "prefer")

    def __repr__(self) -> str:
        return f"<Connection {self.id} {self.connector_type} {self.name}>"
