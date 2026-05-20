"""Add owner, 2FA, org security, and welcome email tracking columns.

Revision ID: j5k6l7m8n9o0
Revises: i4j5k6l7m8n9
Create Date: 2026-05-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "j5k6l7m8n9o0"
down_revision: Union[str, None] = "i4j5k6l7m8n9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("require_2fa", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "organizations",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("welcome_email_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_organizations_owner_user_id",
        "organizations",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column("users", sa.Column("totp_secret_enc", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("totp_enabled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "users",
        sa.Column("totp_recovery_codes", postgresql.JSONB(), nullable=True),
    )

    op.execute(
        """
        UPDATE organizations o
        SET owner_user_id = sub.id
        FROM (
            SELECT DISTINCT ON (u.org_id) u.org_id, u.id
            FROM users u
            WHERE u.role = 'admin'
            ORDER BY u.org_id, u.created_at ASC
        ) sub
        WHERE o.id = sub.org_id AND o.owner_user_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_constraint("fk_organizations_owner_user_id", "organizations", type_="foreignkey")
    op.drop_column("users", "totp_recovery_codes")
    op.drop_column("users", "totp_enabled_at")
    op.drop_column("users", "totp_secret_enc")
    op.drop_column("organizations", "welcome_email_sent_at")
    op.drop_column("organizations", "deleted_at")
    op.drop_column("organizations", "require_2fa")
    op.drop_column("organizations", "owner_user_id")
