"""pipeline_runs table for durable run history

Revision ID: 4c2f8a1d9b07
Revises: 1b945040566e
Create Date: 2026-05-12 22:18:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "4c2f8a1d9b07"
down_revision: Union[str, None] = "1b945040566e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pipeline_runs",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="queued",
            nullable=False,
        ),
        sa.Column(
            "trigger_source",
            sa.String(length=20),
            server_default="manual",
            nullable=False,
        ),
        sa.Column("current_step", sa.String(length=50), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("entities_scored", sa.Integer(), server_default="0", nullable=False),
        sa.Column("critical_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("high_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "recommendations_generated",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("total_llm_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_tool_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "provider_fallbacks", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "step_metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "generation_caps", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
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
    )
    op.create_index(
        "ix_pipeline_runs_org_id", "pipeline_runs", ["org_id"], unique=False
    )
    op.create_index(
        "ix_pipeline_runs_status", "pipeline_runs", ["status"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_pipeline_runs_status", table_name="pipeline_runs")
    op.drop_index("ix_pipeline_runs_org_id", table_name="pipeline_runs")
    op.drop_table("pipeline_runs")
