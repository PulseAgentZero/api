"""agent_memory table for inter-run agent caches

Revision ID: 7e3a91c4bd28
Revises: 4c2f8a1d9b07
Create Date: 2026-05-12 22:34:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "7e3a91c4bd28"
down_revision: Union[str, None] = "4c2f8a1d9b07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_memory",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("agent_name", sa.String(length=100), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "agent_name", name="uq_agent_memory_org_agent"),
    )
    op.create_index(
        "ix_agent_memory_org_id", "agent_memory", ["org_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_agent_memory_org_id", table_name="agent_memory")
    op.drop_table("agent_memory")
