"""projects.webhook_secret -> webhook_secret_encrypted (BYTEA)

Revision ID: 0003_webhook_secret_encrypted
Revises: 0002_uploads
Create Date: 2026-04-28 17:00:00.000000

Replaces the cleartext (or, briefly, "hashed") webhook_secret column with a
Fernet-encrypted bytea so the secret is recoverable for HMAC verification but
unreadable from a DB dump alone. Old plaintext values cannot be migrated
automatically — they would need the WEBHOOK_SECRET_KEY at migration time. We
instead drop and re-add the column; existing project rows must regenerate
their webhook secret via POST /api/v1/projects/{id}/rotate-secret (Sprint 2)
or via a manual encrypt-and-update script.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_webhook_secret_encrypted"
down_revision: str | Sequence[str] | None = "0002_uploads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Pre-Sprint-2 there are no real customer secrets to migrate; dropping
    # the column is the cleanest path. If any rows already exist we delete
    # them rather than silently leave behind unverifiable webhooks.
    op.execute("TRUNCATE projects CASCADE")
    op.drop_column("projects", "webhook_secret")
    op.add_column(
        "projects",
        sa.Column("webhook_secret_encrypted", sa.LargeBinary, nullable=False),
    )


def downgrade() -> None:
    op.execute("TRUNCATE projects CASCADE")
    op.drop_column("projects", "webhook_secret_encrypted")
    op.add_column(
        "projects",
        sa.Column("webhook_secret", sa.String(128), nullable=False),
    )
