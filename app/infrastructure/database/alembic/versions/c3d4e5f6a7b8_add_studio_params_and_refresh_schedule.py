"""Add params, refresh_cron, refresh_enabled to studio_queries

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-05-17

"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "studio_queries",
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "studio_queries",
        sa.Column("refresh_cron", sa.String(100), nullable=True),
    )
    op.add_column(
        "studio_queries",
        sa.Column(
            "refresh_enabled",
            sa.Boolean,
            nullable=False,
            server_default="false",
        ),
    )
    # Index to efficiently load all scheduled queries at startup
    op.create_index(
        "idx_studio_queries_refresh_enabled",
        "studio_queries",
        ["refresh_enabled"],
        postgresql_where=sa.text("refresh_enabled = true"),
    )


def downgrade() -> None:
    op.drop_index("idx_studio_queries_refresh_enabled", table_name="studio_queries")
    op.drop_column("studio_queries", "refresh_enabled")
    op.drop_column("studio_queries", "refresh_cron")
    op.drop_column("studio_queries", "params")
