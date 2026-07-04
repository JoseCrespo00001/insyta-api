"""notifications: feed real de eventos para la campana

Nueva tabla `notifications` (kind/title/detail/link/is_read + event_key para
dedupe) con RLS multi-tenant + soft-delete. Aditiva.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-07-04 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: str | Sequence[str] | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("public_id", sa.String(length=64), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("link", sa.String(length=300), nullable=True),
        sa.Column("is_read", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("event_key", sa.String(length=200), nullable=True),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "is_deleted", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_notifications_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_notifications_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        sa.UniqueConstraint("public_id", name=op.f("uq_notifications_public_id")),
        sa.UniqueConstraint("org_id", "event_key", name="uq_notifications_org_event"),
    )
    op.create_index(
        "ix_notifications_org_id_created_at",
        "notifications",
        ["org_id", "created_at"],
    )
    op.create_index(
        "ix_notifications_org_id_is_read", "notifications", ["org_id", "is_read"]
    )
    op.create_index("ix_notifications_is_deleted", "notifications", ["is_deleted"])

    op.execute("ALTER TABLE notifications ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE notifications FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY org_isolation ON notifications "
        "USING (org_id = current_setting('app.current_org', true)::uuid)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS org_isolation ON notifications")
    op.drop_index("ix_notifications_is_deleted", table_name="notifications")
    op.drop_index("ix_notifications_org_id_is_read", table_name="notifications")
    op.drop_index("ix_notifications_org_id_created_at", table_name="notifications")
    op.drop_table("notifications")
