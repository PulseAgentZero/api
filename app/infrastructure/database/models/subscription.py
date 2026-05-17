from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.base import Base, TimestampMixin, UUIDMixin, utcnow

if TYPE_CHECKING:
    from app.infrastructure.database.models.organization import Organization


class Subscription(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "subscriptions"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    plan: Mapped[str] = mapped_column(String(50), nullable=False, default="free", server_default="free")
    # active | non-renewing | attention | completed | cancelled | inactive
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="inactive", server_default="inactive")

    paystack_customer_code: Mapped[str | None] = mapped_column(Text)
    paystack_subscription_code: Mapped[str | None] = mapped_column(Text, index=True)
    paystack_email_token: Mapped[str | None] = mapped_column(Text)
    paystack_plan_code: Mapped[str | None] = mapped_column(Text)
    authorization_code: Mapped[str | None] = mapped_column(Text)
    next_payment_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), onupdate=utcnow
    )

    organization: Mapped[Organization] = relationship("Organization", lazy="raise")

    def __repr__(self) -> str:
        return f"<Subscription org={self.org_id} plan={self.plan} status={self.status}>"
