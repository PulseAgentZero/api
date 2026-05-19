"""License keys issued by the Pulse license server (cloud)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Integer, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.base import Base, TimestampMixin, UUIDMixin, utcnow

if TYPE_CHECKING:
    from app.infrastructure.database.models.license_activation import LicenseActivation


class LicenseIssuance(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "license_issuances"

    jti: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    payment_reference: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    purchaser_org_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    product: Mapped[str] = mapped_column(Text, default="self_hosted", server_default="self_hosted")
    plan: Mapped[str] = mapped_column(Text, default="pro", server_default="pro")
    features: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    seat_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    license_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), onupdate=utcnow
    )

    activation: Mapped[LicenseActivation | None] = relationship(
        "LicenseActivation",
        back_populates="issuance",
        uselist=False,
        lazy="raise",
    )
