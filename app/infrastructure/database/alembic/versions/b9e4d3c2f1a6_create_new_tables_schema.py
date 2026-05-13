"""create new tables per SCHEMA.md (tables 9-22)

Revision ID: b9e4d3c2f1a6
Revises: a8f3c2d1e4b5
Create Date: 2026-05-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "b9e4d3c2f1a6"
down_revision: Union[str, None] = "a8f3c2d1e4b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "invitations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("invited_by", sa.UUID(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), server_default="analyst", nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token"),
    )
    op.create_index("idx_invitations_org_id", "invitations", ["org_id"], unique=False)
    op.create_index("idx_invitations_token", "invitations", ["token"], unique=False)
    op.create_index(
        "idx_invitations_email",
        "invitations",
        ["org_id", "email"],
        unique=False,
        postgresql_where=sa.text("accepted_at IS NULL"),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("key_prefix", sa.Text(), nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), server_default="read", nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )
    op.create_index(
        "idx_api_keys_org_id",
        "api_keys",
        ["org_id"],
        unique=False,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index("idx_api_keys_key_hash", "api_keys", ["key_hash"], unique=False)

    op.create_table(
        "pipeline_schedules",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("mapping_id", sa.UUID(), nullable=True),
        sa.Column("cron_expression", sa.Text(), server_default="0 */6 * * *", nullable=False),
        sa.Column("timezone", sa.Text(), server_default="UTC", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["mapping_id"], ["schema_mappings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_pipeline_schedules_org_id", "pipeline_schedules", ["org_id"], unique=False
    )
    op.create_index(
        "idx_pipeline_schedules_next_run",
        "pipeline_schedules",
        ["next_run_at"],
        unique=False,
        postgresql_where=sa.text("is_active = TRUE"),
    )

    op.create_table(
        "entity_profiles",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("pipeline_run_id", sa.UUID(), nullable=True),
        sa.Column("mapping_id", sa.UUID(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("entity_name", sa.Text(), nullable=True),
        sa.Column("segment", sa.Text(), nullable=True),
        sa.Column(
            "profile_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("risk_score", sa.Numeric(4, 3), nullable=True),
        sa.Column("risk_tier", sa.Text(), nullable=True),
        sa.Column("risk_narrative", sa.Text(), nullable=True),
        sa.Column("is_latest", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["mapping_id"], ["schema_mappings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pipeline_run_id"], ["pipeline_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_entity_profiles_latest",
        "entity_profiles",
        ["org_id", "entity_id"],
        unique=False,
        postgresql_where=sa.text("is_latest = TRUE"),
    )
    op.create_index(
        "idx_entity_profiles_risk",
        "entity_profiles",
        ["org_id", "risk_tier"],
        unique=False,
        postgresql_where=sa.text("is_latest = TRUE"),
    )
    op.create_index(
        "idx_entity_profiles_segment",
        "entity_profiles",
        ["org_id", "segment"],
        unique=False,
        postgresql_where=sa.text("is_latest = TRUE"),
    )

    op.create_table(
        "entity_risk_history",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("pipeline_run_id", sa.UUID(), nullable=True),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("risk_score", sa.Numeric(4, 3), nullable=False),
        sa.Column("risk_tier", sa.Text(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pipeline_run_id"], ["pipeline_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_risk_history_entity",
        "entity_risk_history",
        ["org_id", "entity_id", "recorded_at"],
        unique=False,
    )
    op.create_index(
        "idx_risk_history_org_date",
        "entity_risk_history",
        ["org_id", "recorded_at"],
        unique=False,
    )

    op.create_table(
        "notification_channels",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("config", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
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
    op.create_index(
        "idx_notification_channels_org_id",
        "notification_channels",
        ["org_id"],
        unique=False,
    )

    op.create_table(
        "alert_rules",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("operator", sa.Text(), nullable=False),
        sa.Column("threshold", sa.Numeric(), nullable=False),
        sa.Column(
            "entity_filter",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "channel_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("cooldown_minutes", sa.Integer(), server_default="60", nullable=False),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_alert_rules_org_id",
        "alert_rules",
        ["org_id"],
        unique=False,
        postgresql_where=sa.text("is_active = TRUE"),
    )

    op.create_table(
        "alert_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("rule_id", sa.UUID(), nullable=False),
        sa.Column("pipeline_run_id", sa.UUID(), nullable=True),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("metric_value", sa.Numeric(), nullable=False),
        sa.Column("threshold", sa.Numeric(), nullable=False),
        sa.Column("affected_entity_ids", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("affected_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pipeline_run_id"], ["pipeline_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["rule_id"], ["alert_rules.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_alert_events_org_id", "alert_events", ["org_id", "created_at"], unique=False
    )
    op.create_index("idx_alert_events_rule_id", "alert_events", ["rule_id"], unique=False)

    op.create_table(
        "notifications",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("type", sa.Text(), server_default="info", nullable=False),
        sa.Column("action_url", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("source_id", sa.UUID(), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_notifications_user",
        "notifications",
        ["user_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("read_at IS NULL"),
    )
    op.create_index(
        "idx_notifications_org",
        "notifications",
        ["org_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("user_id IS NULL"),
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("channel_id", sa.UUID(), nullable=False),
        sa.Column("alert_event_id", sa.UUID(), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["alert_event_id"], ["alert_events.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["channel_id"], ["notification_channels.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_webhook_deliveries_org",
        "webhook_deliveries",
        ["org_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "idx_webhook_deliveries_retry",
        "webhook_deliveries",
        ["next_retry_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending' AND next_retry_at IS NOT NULL"),
    )

    op.create_table(
        "license_keys",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("license_key", sa.Text(), nullable=False),
        sa.Column("plan", sa.Text(), server_default="free", nullable=False),
        sa.Column(
            "features",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("seat_limit", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("validation_cached_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
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
        sa.UniqueConstraint("license_key"),
        sa.UniqueConstraint("org_id"),
    )

    op.create_table(
        "llm_key_store",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("keys", sa.Text(), nullable=False),
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
        "audit_logs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=True),
        sa.Column("resource_id", sa.UUID(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_audit_logs_org_id", "audit_logs", ["org_id", "created_at"], unique=False
    )
    op.create_index(
        "idx_audit_logs_user_id", "audit_logs", ["user_id", "created_at"], unique=False
    )
    op.create_index(
        "idx_audit_logs_action", "audit_logs", ["org_id", "action", "created_at"], unique=False
    )
    op.create_index(
        "idx_audit_logs_resource",
        "audit_logs",
        ["org_id", "resource", "resource_id"],
        unique=False,
    )

    op.create_table(
        "usage_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_usage_events_org",
        "usage_events",
        ["org_id", "event_type", "recorded_at"],
        unique=False,
    )
    op.create_index(
        "idx_usage_events_monthly", "usage_events", ["org_id", "recorded_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("idx_usage_events_monthly", table_name="usage_events")
    op.drop_index("idx_usage_events_org", table_name="usage_events")
    op.drop_table("usage_events")

    op.drop_index("idx_audit_logs_resource", table_name="audit_logs")
    op.drop_index("idx_audit_logs_action", table_name="audit_logs")
    op.drop_index("idx_audit_logs_user_id", table_name="audit_logs")
    op.drop_index("idx_audit_logs_org_id", table_name="audit_logs")
    op.drop_table("audit_logs")

    op.drop_table("llm_key_store")
    op.drop_table("license_keys")

    op.drop_index("idx_webhook_deliveries_retry", table_name="webhook_deliveries")
    op.drop_index("idx_webhook_deliveries_org", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")

    op.drop_index("idx_notifications_org", table_name="notifications")
    op.drop_index("idx_notifications_user", table_name="notifications")
    op.drop_table("notifications")

    op.drop_index("idx_alert_events_rule_id", table_name="alert_events")
    op.drop_index("idx_alert_events_org_id", table_name="alert_events")
    op.drop_table("alert_events")

    op.drop_index("idx_alert_rules_org_id", table_name="alert_rules")
    op.drop_table("alert_rules")

    op.drop_index("idx_notification_channels_org_id", table_name="notification_channels")
    op.drop_table("notification_channels")

    op.drop_index("idx_risk_history_org_date", table_name="entity_risk_history")
    op.drop_index("idx_risk_history_entity", table_name="entity_risk_history")
    op.drop_table("entity_risk_history")

    op.drop_index("idx_entity_profiles_segment", table_name="entity_profiles")
    op.drop_index("idx_entity_profiles_risk", table_name="entity_profiles")
    op.drop_index("idx_entity_profiles_latest", table_name="entity_profiles")
    op.drop_table("entity_profiles")

    op.drop_index("idx_pipeline_schedules_next_run", table_name="pipeline_schedules")
    op.drop_index("idx_pipeline_schedules_org_id", table_name="pipeline_schedules")
    op.drop_table("pipeline_schedules")

    op.drop_index("idx_api_keys_key_hash", table_name="api_keys")
    op.drop_index("idx_api_keys_org_id", table_name="api_keys")
    op.drop_table("api_keys")

    op.drop_index("idx_invitations_email", table_name="invitations")
    op.drop_index("idx_invitations_token", table_name="invitations")
    op.drop_index("idx_invitations_org_id", table_name="invitations")
    op.drop_table("invitations")
