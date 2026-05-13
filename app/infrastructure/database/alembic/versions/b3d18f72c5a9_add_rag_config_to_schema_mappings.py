"""add rag_config to schema_mappings

Revision ID: b3d18f72c5a9
Revises: 2a9c4f8e3d71
Create Date: 2026-05-13 16:10:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "b3d18f72c5a9"
down_revision: Union[str, None] = "2a9c4f8e3d71"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "schema_mappings",
        sa.Column("rag_config", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("schema_mappings", "rag_config")
