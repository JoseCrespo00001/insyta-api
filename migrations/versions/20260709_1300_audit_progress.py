"""audits.evaluated_count: progreso incremental de la auditoría

Columna para mostrar la barra de progreso mientras corre (como rows_processed
en uploads). Aditiva.

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-07-09 13:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3b4c5d6e7f8"
down_revision: str | Sequence[str] | None = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audits",
        sa.Column(
            "evaluated_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("audits", "evaluated_count")
