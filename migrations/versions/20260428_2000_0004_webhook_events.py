"""webhook_events audit table + RLS

Revision ID: 0004_webhook_events
Revises: 0003_webhook_secret_encrypted
Create Date: 2026-04-28 20:00:00.000000

Persists every inbound webhook HTTP POST as one row. See ADR 0001.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_webhook_events"
down_revision: str | Sequence[str] | None = "0003_webhook_secret_encrypted"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_events",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column(
            "project_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("payload", sa.dialects.postgresql.JSONB, nullable=False),
        sa.Column(
            "signature_verified",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("error_message", sa.Text),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_webhook_events_project_idem",
        ),
        sa.CheckConstraint(
            "status IN ('pending','processed','failed')",
            name="ck_webhook_events_status",
        ),
    )
    op.create_index(
        "ix_webhook_events_project_received_at",
        "webhook_events",
        ["project_id", sa.text("received_at DESC")],
    )
    op.create_index("ix_webhook_events_status", "webhook_events", ["status"])

    op.execute("ALTER TABLE webhook_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE webhook_events FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY org_isolation ON webhook_events "
        "USING (org_id = current_setting('app.current_org', true)::uuid)"
    )
    op.execute(
        "CREATE POLICY project_visibility ON webhook_events "
        "USING (project_id = ANY("
        "    string_to_array("
        "        current_setting('app.allowed_projects', true), ','"
        "    )::uuid[]"
        "))"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS project_visibility ON webhook_events")
    op.execute("DROP POLICY IF EXISTS org_isolation ON webhook_events")
    op.execute("ALTER TABLE webhook_events DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_webhook_events_status", table_name="webhook_events")
    op.drop_index("ix_webhook_events_project_received_at", table_name="webhook_events")
    op.drop_table("webhook_events")
