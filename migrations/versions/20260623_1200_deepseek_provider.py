"""add deepseek key + audits.provider

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-23 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("deepseek_api_key_encrypted", sa.Text(), nullable=True),
    )
    op.add_column("audits", sa.Column("provider", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("audits", "provider")
    op.drop_column("organizations", "deepseek_api_key_encrypted")
