"""Studio batch features: filters, tags, panels, column_formats, stars

Revision ID: e1f2a3b4c5d6
Revises: d4e5f6a7b8c9
Create Date: 2026-05-18
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── studio_dashboards ─────────────────────────────────────────────────────
    op.add_column(
        "studio_dashboards",
        sa.Column(
            "dashboard_params",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "studio_dashboards",
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )

    # ── studio_queries ────────────────────────────────────────────────────────
    op.add_column(
        "studio_queries",
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )

    # ── studio_dashboard_items ────────────────────────────────────────────────
    op.add_column(
        "studio_dashboard_items",
        sa.Column(
            "panel_type", sa.String(20), nullable=False, server_default="visualization"
        ),
    )
    op.add_column(
        "studio_dashboard_items",
        sa.Column("content", sa.Text, nullable=True),
    )

    # ── studio_visualizations ─────────────────────────────────────────────────
    op.add_column(
        "studio_visualizations",
        sa.Column(
            "column_formats",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )

    # ── studio_stars (new table) ──────────────────────────────────────────────
    op.create_table(
        "studio_stars",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("resource_type", sa.String(20), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "user_id", "resource_type", "resource_id", name="uq_studio_star"
        ),
    )
    op.create_index(
        "idx_studio_stars_user_resource", "studio_stars", ["user_id", "resource_type"]
    )
    op.create_index("idx_studio_stars_org", "studio_stars", ["org_id"])

    # GIN indexes for tag containment queries
    op.create_index(
        "idx_studio_queries_tags",
        "studio_queries",
        ["tags"],
        postgresql_using="gin",
    )
    op.create_index(
        "idx_studio_dashboards_tags",
        "studio_dashboards",
        ["tags"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("idx_studio_dashboards_tags", table_name="studio_dashboards")
    op.drop_index("idx_studio_queries_tags", table_name="studio_queries")
    op.drop_index("idx_studio_stars_org", table_name="studio_stars")
    op.drop_index("idx_studio_stars_user_resource", table_name="studio_stars")
    op.drop_table("studio_stars")
    op.drop_column("studio_visualizations", "column_formats")
    op.drop_column("studio_dashboard_items", "content")
    op.drop_column("studio_dashboard_items", "panel_type")
    op.drop_column("studio_queries", "tags")
    op.drop_column("studio_dashboards", "tags")
    op.drop_column("studio_dashboards", "dashboard_params")
