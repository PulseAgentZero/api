"""merge analytics_exports head with rag_config head

Revision ID: e8f9a0b1c2d3
Revises: b3d18f72c5a9, d1e2f3a4b5c6
Create Date: 2026-05-14

Resolves parallel migration branches so ``alembic upgrade head`` works (Docker API entrypoint).
"""

from typing import Sequence, Union

revision: str = "e8f9a0b1c2d3"
down_revision: Union[str, tuple[str, ...], None] = ("b3d18f72c5a9", "d1e2f3a4b5c6")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
