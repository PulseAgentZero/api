"""First activation binding for a license issuance (single-use per key)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.base import Base, TimestampMixin, UUIDMixin, utcnow

if TYPE_CHECKING:
    from app.infrastructure.database.models.license_issuance import LicenseIssuance


class LicenseActivation(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "license_activations"

    issuance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("license_issuances.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    bound_org_id: Mapped[str] = mapped_column(Text, nullable=False)
    first_activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), onupdate=utcnow
    )

    issuance: Mapped[LicenseIssuance] = relationship("LicenseIssuance", back_populates="activation", lazy="raise")
