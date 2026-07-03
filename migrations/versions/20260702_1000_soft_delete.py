"""soft_delete: is_deleted + deleted_at en entidades borrables

Agrega borrado lógico a las entidades soft-deletables y su subtree. Aditiva y no
destructiva: is_deleted BOOLEAN NOT NULL DEFAULT false (backfill automático de
filas existentes a false) + deleted_at TIMESTAMPTZ NULL + índice por is_deleted.
Los endpoints DELETE ahora setean is_deleted=True en vez de borrar filas.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-02 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str | Sequence[str] | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tablas soft-deletables (raíces con endpoint DELETE + su subtree). NO incluye
# organizations ni users (no son borrables por el usuario).
_TABLES: tuple[str, ...] = (
    "projects",
    "agents",
    "conversations",
    "messages",
    "evaluations",
    "uploads",
    "flows",
    "flow_versions",
    "audits",
    "audit_conversations",
    "message_evaluations",
    "improvements",
    "improvement_conversations",
)


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "is_deleted",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "deleted_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
        op.create_index(
            f"ix_{table}_is_deleted",
            table,
            ["is_deleted"],
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_index(f"ix_{table}_is_deleted", table_name=table)
        op.drop_column(table, "deleted_at")
        op.drop_column(table, "is_deleted")
