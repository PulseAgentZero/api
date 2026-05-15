"""add rag_metrics to pipeline_runs

Adds a JSONB column to store per-run RAG telemetry: latency stats, eval
regression results, and TTL cleanup counts persisted by the pipeline orchestrator.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e8d7c6b5a493"
down_revision: Union[str, tuple[str, ...], None] = "f9e8d7c6b5a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "pipeline_runs",
        sa.Column("rag_metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pipeline_runs", "rag_metrics")
