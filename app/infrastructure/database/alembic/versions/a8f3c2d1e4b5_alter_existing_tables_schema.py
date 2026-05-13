"""alter existing tables per SCHEMA.md (tables 1-8)

Revision ID: a8f3c2d1e4b5
Revises: 7e3a91c4bd28
Create Date: 2026-05-13

Adds columns; does not drop legacy connection columns (see migration 6 later).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a8f3c2d1e4b5"
down_revision: Union[str, None] = "7e3a91c4bd28"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- organizations ---
    op.add_column("organizations", sa.Column("slug", sa.Text(), nullable=True))
    op.add_column(
        "organizations",
        sa.Column("plan", sa.Text(), server_default="free", nullable=False),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "deployment_mode",
            sa.Text(),
            server_default="cloud",
            nullable=False,
        ),
    )
    op.add_column(
        "organizations",
        sa.Column("timezone", sa.Text(), server_default="UTC", nullable=False),
    )
    op.add_column("organizations", sa.Column("logo_url", sa.Text(), nullable=True))
    op.add_column(
        "organizations",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_organizations_slug",
        "organizations",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("slug IS NOT NULL"),
    )

    # --- users ---
    op.alter_column(
        "users",
        "password_hash",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.add_column(
        "users",
        sa.Column("full_name", sa.Text(), server_default="", nullable=False),
    )
    op.add_column(
        "users",
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
    )
    op.add_column(
        "users",
        sa.Column("is_verified", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "users",
        sa.Column(
            "auth_provider",
            sa.Text(),
            server_default="email",
            nullable=False,
        ),
    )
    op.add_column("users", sa.Column("auth_provider_id", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("idx_users_org_id", "users", ["org_id"], unique=False)
    op.create_index("idx_users_email", "users", ["email"], unique=False)
    op.create_index(
        "idx_users_oauth",
        "users",
        ["auth_provider", "auth_provider_id"],
        unique=False,
        postgresql_where=sa.text("auth_provider_id IS NOT NULL"),
    )

    # --- connections ---
    op.add_column(
        "connections",
        sa.Column("name", sa.Text(), server_default="My Connection", nullable=False),
    )
    op.add_column(
        "connections",
        sa.Column(
            "connector_type",
            sa.Text(),
            server_default="postgres",
            nullable=False,
        ),
    )
    op.add_column("connections", sa.Column("credentials", sa.Text(), nullable=True))
    op.add_column(
        "connections",
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "connections",
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("connections", sa.Column("last_test_error", sa.Text(), nullable=True))
    op.add_column("connections", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "connections",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_connections_org_id",
        "connections",
        ["org_id"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # --- schema_mappings ---
    op.add_column(
        "schema_mappings",
        sa.Column("name", sa.Text(), server_default="Default", nullable=False),
    )
    op.add_column(
        "schema_mappings",
        sa.Column(
            "entity_type",
            sa.Text(),
            server_default="customer",
            nullable=False,
        ),
    )
    op.add_column("schema_mappings", sa.Column("segment_column", sa.Text(), nullable=True))
    op.add_column(
        "schema_mappings",
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
    )
    op.add_column("schema_mappings", sa.Column("goal", sa.Text(), nullable=True))
    op.add_column(
        "schema_mappings",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_schema_mappings_org_id", "schema_mappings", ["org_id"], unique=False
    )
    op.create_index(
        "idx_schema_mappings_connection_id",
        "schema_mappings",
        ["connection_id"],
        unique=False,
    )

    # --- recommendations ---
    op.add_column(
        "recommendations",
        sa.Column("pipeline_run_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_recommendations_pipeline_run",
        "recommendations",
        "pipeline_runs",
        ["pipeline_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("recommendations", sa.Column("confidence_score", sa.Numeric(4, 3), nullable=True))
    op.add_column("recommendations", sa.Column("expected_impact", sa.Text(), nullable=True))
    op.add_column("recommendations", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("recommendations", sa.Column("outcome_notes", sa.Text(), nullable=True))
    op.add_column(
        "recommendations",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_recommendations_org_id", "recommendations", ["org_id"], unique=False
    )
    op.create_index(
        "idx_recommendations_entity",
        "recommendations",
        ["org_id", "entity_id"],
        unique=False,
    )
    op.create_index(
        "idx_recommendations_status",
        "recommendations",
        ["org_id", "status"],
        unique=False,
    )
    op.create_index(
        "idx_recommendations_urgency",
        "recommendations",
        ["org_id", "urgency", "status"],
        unique=False,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index(
        "idx_recommendations_run",
        "recommendations",
        ["pipeline_run_id"],
        unique=False,
    )

    # --- agent_conversations ---
    op.add_column("agent_conversations", sa.Column("title", sa.Text(), nullable=True))
    op.add_column(
        "agent_conversations",
        sa.Column("message_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "agent_conversations", sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "agent_conversations", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        "UPDATE agent_conversations SET messages = '[]'::jsonb WHERE messages IS NULL"
    )
    op.alter_column(
        "agent_conversations",
        "messages",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    )
    op.create_index(
        "idx_conversations_org_user",
        "agent_conversations",
        ["org_id", "user_id"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "idx_conversations_updated",
        "agent_conversations",
        ["org_id", "updated_at"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # --- pipeline_runs ---
    op.add_column("pipeline_runs", sa.Column("mapping_id", sa.UUID(), nullable=True))
    op.add_column("pipeline_runs", sa.Column("triggered_by", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_pipeline_runs_mapping",
        "pipeline_runs",
        "schema_mappings",
        ["mapping_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_pipeline_runs_triggered_by",
        "pipeline_runs",
        "users",
        ["triggered_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "idx_pipeline_runs_created_at",
        "pipeline_runs",
        ["org_id", "created_at"],
        unique=False,
    )

    # --- agent_memory: drop old unique, add scope columns ---
    op.drop_constraint("uq_agent_memory_org_agent", "agent_memory", type_="unique")
    op.add_column(
        "agent_memory",
        sa.Column("scope", sa.Text(), server_default="org", nullable=False),
    )
    op.add_column("agent_memory", sa.Column("scope_id", sa.UUID(), nullable=True))
    op.add_column("agent_memory", sa.Column("key", sa.Text(), nullable=True))
    op.add_column(
        "agent_memory", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "idx_agent_memory_org", "agent_memory", ["org_id", "scope"], unique=False
    )
    op.create_index(
        "idx_agent_memory_scope_id",
        "agent_memory",
        ["scope_id"],
        unique=False,
        postgresql_where=sa.text("scope_id IS NOT NULL"),
    )
    op.create_index(
        "idx_agent_memory_expires",
        "agent_memory",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )

    op.execute("UPDATE recommendations SET status = 'open' WHERE status = 'active'")
    op.execute("UPDATE connections SET status = 'pending' WHERE status = 'untested'")


def downgrade() -> None:
    op.drop_index("idx_agent_memory_expires", table_name="agent_memory")
    op.drop_index("idx_agent_memory_scope_id", table_name="agent_memory")
    op.drop_index("idx_agent_memory_org", table_name="agent_memory")
    op.drop_column("agent_memory", "expires_at")
    op.drop_column("agent_memory", "key")
    op.drop_column("agent_memory", "scope_id")
    op.drop_column("agent_memory", "scope")
    op.create_unique_constraint(
        "uq_agent_memory_org_agent", "agent_memory", ["org_id", "agent_name"]
    )

    op.drop_index("idx_pipeline_runs_created_at", table_name="pipeline_runs")
    op.drop_constraint("fk_pipeline_runs_triggered_by", "pipeline_runs", type_="foreignkey")
    op.drop_constraint("fk_pipeline_runs_mapping", "pipeline_runs", type_="foreignkey")
    op.drop_column("pipeline_runs", "triggered_by")
    op.drop_column("pipeline_runs", "mapping_id")

    op.drop_index("idx_conversations_updated", table_name="agent_conversations")
    op.drop_index("idx_conversations_org_user", table_name="agent_conversations")
    op.alter_column(
        "agent_conversations",
        "messages",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        server_default=None,
        nullable=True,
    )
    op.drop_column("agent_conversations", "deleted_at")
    op.drop_column("agent_conversations", "last_message_at")
    op.drop_column("agent_conversations", "message_count")
    op.drop_column("agent_conversations", "title")

    op.drop_index("idx_recommendations_run", table_name="recommendations")
    op.drop_index("idx_recommendations_urgency", table_name="recommendations")
    op.drop_index("idx_recommendations_status", table_name="recommendations")
    op.drop_index("idx_recommendations_entity", table_name="recommendations")
    op.drop_index("idx_recommendations_org_id", table_name="recommendations")
    op.drop_column("recommendations", "updated_at")
    op.drop_column("recommendations", "outcome_notes")
    op.drop_column("recommendations", "expires_at")
    op.drop_column("recommendations", "expected_impact")
    op.drop_column("recommendations", "confidence_score")
    op.drop_constraint("fk_recommendations_pipeline_run", "recommendations", type_="foreignkey")
    op.drop_column("recommendations", "pipeline_run_id")

    op.drop_index("idx_schema_mappings_connection_id", table_name="schema_mappings")
    op.drop_index("idx_schema_mappings_org_id", table_name="schema_mappings")
    op.drop_column("schema_mappings", "updated_at")
    op.drop_column("schema_mappings", "goal")
    op.drop_column("schema_mappings", "is_active")
    op.drop_column("schema_mappings", "segment_column")
    op.drop_column("schema_mappings", "entity_type")
    op.drop_column("schema_mappings", "name")

    op.drop_index("idx_connections_org_id", table_name="connections")
    op.drop_column("connections", "updated_at")
    op.drop_column("connections", "deleted_at")
    op.drop_column("connections", "last_test_error")
    op.drop_column("connections", "metadata")
    op.drop_column("connections", "config")
    op.drop_column("connections", "credentials")
    op.drop_column("connections", "connector_type")
    op.drop_column("connections", "name")

    op.drop_index("idx_users_oauth", table_name="users")
    op.drop_index("idx_users_email", table_name="users")
    op.drop_index("idx_users_org_id", table_name="users")
    op.drop_column("users", "updated_at")
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "auth_provider_id")
    op.drop_column("users", "auth_provider")
    op.drop_column("users", "is_verified")
    op.drop_column("users", "is_active")
    op.drop_column("users", "full_name")
    op.alter_column(
        "users",
        "password_hash",
        existing_type=sa.String(length=255),
        nullable=False,
    )

    op.drop_index("idx_organizations_slug", table_name="organizations")
    op.drop_column("organizations", "updated_at")
    op.drop_column("organizations", "logo_url")
    op.drop_column("organizations", "timezone")
    op.drop_column("organizations", "deployment_mode")
    op.drop_column("organizations", "plan")
    op.drop_column("organizations", "slug")
