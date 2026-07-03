"""upload_raw_content: CSV crudo en Postgres (bytea) + storage_path nullable

El CSV subido ahora se guarda en `uploads.raw_content` (Supabase Postgres) en vez
de disco/Storage. Aditiva: agrega la columna bytea (nullable) y afloja el NOT NULL
de storage_path (legacy). No destructiva.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-07-02 11:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: str | Sequence[str] | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("uploads", sa.Column("raw_content", sa.LargeBinary(), nullable=True))
    op.alter_column(
        "uploads", "storage_path", existing_type=sa.String(512), nullable=True
    )


def downgrade() -> None:
    # storage_path vuelve a NOT NULL: rellenar los NULL con un placeholder antes.
    op.execute("UPDATE uploads SET storage_path = '' WHERE storage_path IS NULL")
    op.alter_column(
        "uploads", "storage_path", existing_type=sa.String(512), nullable=False
    )
    op.drop_column("uploads", "raw_content")
