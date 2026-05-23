from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.base import Base, TimestampMixin, UUIDMixin, utcnow

if TYPE_CHECKING:
    from app.infrastructure.database.models.organization import Organization


class SsoConfiguration(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "sso_configurations"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)  # oidc | saml
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    client_id: Mapped[str | None] = mapped_column(Text)
    client_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    discovery_url: Mapped[str | None] = mapped_column(Text)
    scopes: Mapped[str | None] = mapped_column(Text)
    email_claim: Mapped[str] = mapped_column(Text, default="email", server_default="email")
    name_claim: Mapped[str] = mapped_column(Text, default="name", server_default="name")
    entity_id: Mapped[str | None] = mapped_column(Text)
    metadata_xml: Mapped[str | None] = mapped_column(Text)
    metadata_url: Mapped[str | None] = mapped_column(Text)
    acs_url_path: Mapped[str | None] = mapped_column(Text)
    name_id_format: Mapped[str | None] = mapped_column(Text)
    default_role: Mapped[str] = mapped_column(Text, default="viewer", server_default="viewer")
    auto_provision_users: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )
    allowed_email_domains: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default="{}"
    )
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), onupdate=utcnow
    )

    organization: Mapped[Organization] = relationship("Organization", lazy="raise")
