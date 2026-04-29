"""alerts + projects.alert_webhook_url + conversations.archived_at

Revision ID: 0006_alerts_table
Revises: 0005_alert_thresholds
Create Date: 2026-04-28 20:20:00.000000

- Creates `alerts` (RLS, dedup unique index). See ADR 0003.
- Adds `projects.alert_webhook_url` so customers can receive alerts via HTTP.
- Adds `conversations.archived_at` for the retention worker (EQUIP-97).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_alerts_table"
down_revision: str | Sequence[str] | None = "0005_alert_thresholds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects", sa.Column("alert_webhook_url", sa.String(512), nullable=True)
    )
    op.add_column(
        "conversations",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_conversations_archived_at",
        "conversations",
        ["archived_at"],
        postgresql_where=sa.text("archived_at IS NOT NULL"),
    )

    op.create_table(
        "alerts",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
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
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("dedup_key", sa.String(128), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "payload",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("count", sa.Integer, nullable=False, server_default="1"),
        sa.Column("notified_at", sa.DateTime(timezone=True)),
        sa.Column("notified_via", sa.String(32)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "project_id",
            "type",
            "dedup_key",
            "window_start",
            name="uq_alerts_project_type_dedup_window",
        ),
        sa.CheckConstraint(
            "type IN ('frustration','score_drop','new_topic','escalation')",
            name="ck_alerts_type",
        ),
    )
    op.create_index(
        "ix_alerts_project_type_created_at",
        "alerts",
        ["project_id", "type", sa.text("created_at DESC")],
    )

    op.execute("ALTER TABLE alerts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE alerts FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY org_isolation ON alerts "
        "USING (org_id = current_setting('app.current_org', true)::uuid)"
    )
    op.execute(
        "CREATE POLICY project_visibility ON alerts "
        "USING (project_id = ANY("
        "    string_to_array("
        "        current_setting('app.allowed_projects', true), ','"
        "    )::uuid[]"
        "))"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS project_visibility ON alerts")
    op.execute("DROP POLICY IF EXISTS org_isolation ON alerts")
    op.execute("ALTER TABLE alerts DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_alerts_project_type_created_at", table_name="alerts")
    op.drop_table("alerts")
    op.drop_index(
        "ix_conversations_archived_at",
        table_name="conversations",
        postgresql_where=sa.text("archived_at IS NOT NULL"),
    )
    op.drop_column("conversations", "archived_at")
    op.drop_column("projects", "alert_webhook_url")
