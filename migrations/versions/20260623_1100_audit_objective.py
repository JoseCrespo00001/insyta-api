"""add audits.objective + projects.company_context

Revision ID: b2c3d4e5f6a7
Revises: a1f2c3d4e5f6
Create Date: 2026-06-23 11:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "a1f2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("audits", sa.Column("objective", sa.String(length=32), nullable=True))
    op.add_column("projects", sa.Column("company_context", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "company_context")
    op.drop_column("audits", "objective")
