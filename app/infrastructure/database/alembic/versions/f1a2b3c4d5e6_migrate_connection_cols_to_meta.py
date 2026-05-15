"""migrate connection flat columns into connection_meta JSONB

Revision ID: f9e8d7c6b5a4
Revises: c7d8e9f0a1b2
Create Date: 2026-05-15

Drop db_type, host, port, database_name, username, sslmode as dedicated
columns — all connector-specific fields live in connection_meta JSONB so
new connector types don't need schema changes.
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "f9e8d7c6b5a4"
down_revision: Union[str, tuple[str, ...], None] = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Merge existing column values into connection_meta before dropping them.
    # jsonb_strip_nulls removes keys whose value is SQL NULL so we don't
    # pollute meta with {host: null, port: null, ...} for non-SQL connectors.
    op.execute(
        """
        UPDATE connections
        SET connection_meta = COALESCE(connection_meta, '{}'::jsonb)
            || jsonb_strip_nulls(jsonb_build_object(
                'db_type',       db_type,
                'host',          host,
                'port',          port,
                'database_name', database_name,
                'username',      username,
                'sslmode',       COALESCE(sslmode, 'prefer')
            ))
        """
    )

    op.drop_column("connections", "db_type")
    op.drop_column("connections", "host")
    op.drop_column("connections", "port")
    op.drop_column("connections", "database_name")
    op.drop_column("connections", "username")
    op.drop_column("connections", "sslmode")


def downgrade() -> None:
    op.add_column("connections", sa.Column("db_type", sa.String(20), nullable=True))
    op.add_column("connections", sa.Column("host", sa.Text, nullable=True))
    op.add_column("connections", sa.Column("port", sa.Integer, nullable=True))
    op.add_column("connections", sa.Column("database_name", sa.Text, nullable=True))
    op.add_column("connections", sa.Column("username", sa.String(255), nullable=True))
    op.add_column("connections", sa.Column("sslmode", sa.String(20), nullable=True, server_default="prefer"))

    op.execute(
        """
        UPDATE connections
        SET
            db_type       = (connection_meta->>'db_type'),
            host          = (connection_meta->>'host'),
            port          = (connection_meta->>'port')::int,
            database_name = (connection_meta->>'database_name'),
            username      = (connection_meta->>'username'),
            sslmode       = COALESCE(connection_meta->>'sslmode', 'prefer')
        """
    )
