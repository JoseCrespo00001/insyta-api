"""message_multimodal: message_type + media_transcript (AUD-3.4)

Aditiva: message_type (text|audio|image|doc|location, default 'text') +
media_transcript (nullable) para detectar audio/imagen sin procesar.

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-07-03 03:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d0e1f2a3b4c5"
down_revision: str | Sequence[str] | None = "c9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column(
            "message_type",
            sa.String(16),
            nullable=False,
            server_default="text",
        ),
    )
    op.add_column("messages", sa.Column("media_transcript", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "media_transcript")
    op.drop_column("messages", "message_type")
