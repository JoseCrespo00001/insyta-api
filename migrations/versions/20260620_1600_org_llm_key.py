"""add organizations.anthropic_api_key_encrypted

Revision ID: a1f2c3d4e5f6
Revises: 6364b785ac4e
Create Date: 2026-06-20 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1f2c3d4e5f6"
down_revision: str | Sequence[str] | None = "6364b785ac4e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("anthropic_api_key_encrypted", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organizations", "anthropic_api_key_encrypted")
