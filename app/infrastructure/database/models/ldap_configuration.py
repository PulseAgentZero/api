from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.base import Base, TimestampMixin, UUIDMixin, utcnow

if TYPE_CHECKING:
    from app.infrastructure.database.models.organization import Organization


class LdapConfiguration(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "ldap_configurations"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    server_url: Mapped[str] = mapped_column(Text, nullable=False)
    bind_dn: Mapped[str] = mapped_column(Text, nullable=False)
    bind_password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    user_search_base: Mapped[str] = mapped_column(Text, nullable=False)
    user_search_filter: Mapped[str] = mapped_column(
        Text, default="(objectClass=person)", server_default="(objectClass=person)"
    )
    email_attr: Mapped[str] = mapped_column(Text, default="mail", server_default="mail")
    name_attr: Mapped[str] = mapped_column(Text, default="cn", server_default="cn")
    group_attr: Mapped[str | None] = mapped_column(Text)
    default_role: Mapped[str] = mapped_column(Text, default="viewer", server_default="viewer")
    role_mapping: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    sync_schedule_cron: Mapped[str] = mapped_column(
        Text, default="0 */6 * * *", server_default="0 */6 * * *"
    )
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_status: Mapped[str | None] = mapped_column(Text)
    last_sync_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), onupdate=utcnow
    )

    organization: Mapped[Organization] = relationship("Organization", lazy="raise")
