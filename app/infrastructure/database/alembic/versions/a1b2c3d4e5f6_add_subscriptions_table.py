"""add subscriptions table

Revision ID: a1b2c3d4e5f6
Revises: e8d7c6b5a493
Create Date: 2026-05-17
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "e8d7c6b5a493"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "org_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("plan", sa.String(50), nullable=False, server_default="free"),
        sa.Column("status", sa.String(50), nullable=False, server_default="inactive"),
        sa.Column("paystack_customer_code", sa.Text(), nullable=True),
        sa.Column("paystack_subscription_code", sa.Text(), nullable=True),
        sa.Column("paystack_email_token", sa.Text(), nullable=True),
        sa.Column("paystack_plan_code", sa.Text(), nullable=True),
        sa.Column("authorization_code", sa.Text(), nullable=True),
        sa.Column("next_payment_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_subscriptions_org_id", "subscriptions", ["org_id"])
    op.create_index(
        "ix_subscriptions_paystack_subscription_code",
        "subscriptions",
        ["paystack_subscription_code"],
    )


def downgrade() -> None:
    op.drop_index("ix_subscriptions_paystack_subscription_code", table_name="subscriptions")
    op.drop_index("ix_subscriptions_org_id", table_name="subscriptions")
    op.drop_table("subscriptions")
