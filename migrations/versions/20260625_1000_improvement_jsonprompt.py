"""add improvements.node_json + prompt

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-25 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("improvements", sa.Column("node_json", sa.Text(), nullable=True))
    op.add_column("improvements", sa.Column("prompt", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("improvements", "prompt")
    op.drop_column("improvements", "node_json")
