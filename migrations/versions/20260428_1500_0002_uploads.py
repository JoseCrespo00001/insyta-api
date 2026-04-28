"""uploads table + RLS

Revision ID: 0002_uploads
Revises: 0001_initial
Create Date: 2026-04-28 15:00:00.000000

Tracks CSV upload jobs. Tenant-scoped via (org_id, project_id) so RLS
isolation matches the rest of the data model.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_uploads"
down_revision: str | Sequence[str] | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "uploads",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "org_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
        ),
        sa.Column("filename", sa.String(256), nullable=False),
        sa.Column("storage_path", sa.String(512), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("rows_total", sa.Integer),
        sa.Column("rows_processed", sa.Integer),
        sa.Column("error_message", sa.Text),
        sa.Column("metadata", sa.dialects.postgresql.JSONB),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
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
        sa.CheckConstraint(
            "status IN ('pending','processing','completed','failed')",
            name="ck_uploads_status_enum",
        ),
    )
    op.create_index(
        "ix_uploads_project_id_created_at",
        "uploads",
        ["project_id", sa.text("created_at DESC")],
    )
    op.create_index("ix_uploads_org_id_status", "uploads", ["org_id", "status"])

    # RLS policies: org_isolation + project_visibility, mirroring the other
    # tenant-scoped tables created in 0001_initial.
    op.execute("ALTER TABLE uploads ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE uploads FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY org_isolation ON uploads "
        "USING (org_id = current_setting('app.current_org', true)::uuid)"
    )
    op.execute(
        "CREATE POLICY project_visibility ON uploads "
        "USING (project_id = ANY("
        "    string_to_array("
        "        current_setting('app.allowed_projects', true), ','"
        "    )::uuid[]"
        "))"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS project_visibility ON uploads")
    op.execute("DROP POLICY IF EXISTS org_isolation ON uploads")
    op.execute("ALTER TABLE uploads DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_uploads_org_id_status", table_name="uploads")
    op.drop_index("ix_uploads_project_id_created_at", table_name="uploads")
    op.drop_table("uploads")
