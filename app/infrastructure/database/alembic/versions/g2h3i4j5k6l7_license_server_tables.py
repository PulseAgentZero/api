"""License server issuance and activation tables.

Revision ID: g2h3i4j5k6l7
Revises: e8f9a0b1c2d3
Create Date: 2026-05-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "g2h3i4j5k6l7"
down_revision: Union[str, None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "license_issuances",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("jti", sa.Text(), nullable=False),
        sa.Column("payment_reference", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("purchaser_org_id", sa.Text(), nullable=True),
        sa.Column("product", sa.Text(), server_default="self_hosted", nullable=False),
        sa.Column("plan", sa.Text(), server_default="pro", nullable=False),
        sa.Column(
            "features",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("seat_limit", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("license_key", sa.Text(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("jti"),
        sa.UniqueConstraint("payment_reference"),
        sa.UniqueConstraint("license_key"),
    )
    op.create_table(
        "license_activations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("issuance_id", sa.UUID(), nullable=False),
        sa.Column("bound_org_id", sa.Text(), nullable=False),
        sa.Column("first_activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["issuance_id"], ["license_issuances.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issuance_id"),
    )


def downgrade() -> None:
    op.drop_table("license_activations")
    op.drop_table("license_issuances")
