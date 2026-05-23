"""Self-hosted enterprise: log_streams, sso_configurations, ldap_configurations.

Revision ID: l7m8n9o0p1q2
Revises: k6l7m8n9o0p1
Create Date: 2026-05-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "l7m8n9o0p1q2"
down_revision: Union[str, None] = "k6l7m8n9o0p1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "license_keys",
        sa.Column("limits", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
    )
    op.add_column("users", sa.Column("sso_provider", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("sso_subject", sa.Text(), nullable=True))

    op.create_table(
        "log_streams",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("destination_type", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("min_level", sa.Text(), server_default="INFO", nullable=False),
        sa.Column("event_categories", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_log_streams_org_id", "log_streams", ["org_id"])

    op.create_table(
        "sso_configurations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("client_id", sa.Text(), nullable=True),
        sa.Column("client_secret_encrypted", sa.Text(), nullable=True),
        sa.Column("discovery_url", sa.Text(), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column("email_claim", sa.Text(), server_default="email", nullable=False),
        sa.Column("name_claim", sa.Text(), server_default="name", nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=True),
        sa.Column("metadata_xml", sa.Text(), nullable=True),
        sa.Column("metadata_url", sa.Text(), nullable=True),
        sa.Column("acs_url_path", sa.Text(), nullable=True),
        sa.Column("name_id_format", sa.Text(), nullable=True),
        sa.Column("default_role", sa.Text(), server_default="viewer", nullable=False),
        sa.Column("auto_provision_users", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "allowed_email_domains",
            postgresql.ARRAY(sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("extra", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
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
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id"),
    )

    op.create_table(
        "ldap_configurations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("server_url", sa.Text(), nullable=False),
        sa.Column("bind_dn", sa.Text(), nullable=False),
        sa.Column("bind_password_encrypted", sa.Text(), nullable=False),
        sa.Column("user_search_base", sa.Text(), nullable=False),
        sa.Column("user_search_filter", sa.Text(), server_default="(objectClass=person)", nullable=False),
        sa.Column("email_attr", sa.Text(), server_default="mail", nullable=False),
        sa.Column("name_attr", sa.Text(), server_default="cn", nullable=False),
        sa.Column("group_attr", sa.Text(), nullable=True),
        sa.Column("default_role", sa.Text(), server_default="viewer", nullable=False),
        sa.Column("role_mapping", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("sync_schedule_cron", sa.Text(), server_default="0 */6 * * *", nullable=False),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_status", sa.Text(), nullable=True),
        sa.Column("last_sync_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id"),
    )


def downgrade() -> None:
    op.drop_table("ldap_configurations")
    op.drop_table("sso_configurations")
    op.drop_index("ix_log_streams_org_id", table_name="log_streams")
    op.drop_table("log_streams")
    op.drop_column("users", "sso_subject")
    op.drop_column("users", "sso_provider")
    op.drop_column("license_keys", "limits")
