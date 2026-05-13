"""Add sslmode column to connections

Revision ID: 2a9c4f8e3d71
Revises: 1b945040566e
Create Date: 2026-05-12 17:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "2a9c4f8e3d71"
down_revision: Union[str, None] = "97468adb7cb9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "connections",
        sa.Column(
            "sslmode",
            sa.String(20),
            nullable=False,
            server_default="prefer",
        ),
    )


def downgrade() -> None:
    op.drop_column("connections", "sslmode")
