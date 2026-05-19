"""Add refresh_interval_seconds and time_range to studio_dashboards.

Revision ID: i4j5k6l7m8n9
Revises: h3i4j5k6l7m8
Create Date: 2026-05-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "i4j5k6l7m8n9"
down_revision: Union[str, None] = "h3i4j5k6l7m8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "studio_dashboards",
        sa.Column("refresh_interval_seconds", sa.Integer(), nullable=True),
    )
    op.add_column(
        "studio_dashboards",
        sa.Column(
            "time_range",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("studio_dashboards", "time_range")
    op.drop_column("studio_dashboards", "refresh_interval_seconds")
