"""Add Pulse Studio tables (studio_queries, studio_visualizations, studio_dashboards, studio_dashboard_items)

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-17

"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── studio_queries ────────────────────────────────────────────────────────
    op.create_table(
        "studio_queries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("connections.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("sql_text", sa.Text, nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_row_count", sa.Integer, nullable=True),
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
    op.create_index("idx_studio_queries_org_id", "studio_queries", ["org_id"])
    op.create_index(
        "idx_studio_queries_org_connection",
        "studio_queries",
        ["org_id", "connection_id"],
    )

    # ── studio_visualizations ─────────────────────────────────────────────────
    op.create_table(
        "studio_visualizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "query_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("studio_queries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column(
            "chart_type",
            sa.String(20),
            nullable=False,
            server_default="table",
        ),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
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
    op.create_index("idx_studio_visualizations_org_id", "studio_visualizations", ["org_id"])
    op.create_index(
        "idx_studio_visualizations_query_id", "studio_visualizations", ["query_id"]
    )

    # ── studio_dashboards ─────────────────────────────────────────────────────
    op.create_table(
        "studio_dashboards",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("slug", sa.String(100), nullable=True, unique=True),
        sa.Column(
            "is_public",
            sa.Boolean,
            nullable=False,
            server_default="false",
        ),
        sa.Column(
            "layout",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
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
    op.create_index("idx_studio_dashboards_org_id", "studio_dashboards", ["org_id"])
    # Partial unique index on slug (only where slug is not null)
    op.create_index(
        "idx_studio_dashboards_slug",
        "studio_dashboards",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("slug IS NOT NULL"),
    )
    # Partial index to speed up public dashboard lookups
    op.create_index(
        "idx_studio_dashboards_public",
        "studio_dashboards",
        ["is_public", "slug"],
        postgresql_where=sa.text("is_public = true"),
    )

    # ── studio_dashboard_items ────────────────────────────────────────────────
    op.create_table(
        "studio_dashboard_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "dashboard_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("studio_dashboards.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "visualization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("studio_visualizations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("position", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_studio_dashboard_items_dashboard_id",
        "studio_dashboard_items",
        ["dashboard_id"],
    )
    op.create_index(
        "idx_studio_dashboard_items_org_id",
        "studio_dashboard_items",
        ["org_id"],
    )


def downgrade() -> None:
    op.drop_table("studio_dashboard_items")
    op.drop_table("studio_dashboards")
    op.drop_table("studio_visualizations")
    op.drop_table("studio_queries")
