"""flow_versions: historial de versiones del flujo + RLS

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-26 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5f6a7b8c9d0"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "flow_versions",
        sa.Column("public_id", sa.String(length=64), nullable=False),
        sa.Column("flow_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'manual'"),
        ),
        sa.Column("flow_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "size_bytes", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "agent_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
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
        sa.CheckConstraint(
            "source IN ('initial','improvement','manual','restore','upload')",
            name=op.f("ck_flow_versions_source_enum"),
        ),
        sa.ForeignKeyConstraint(
            ["flow_id"],
            ["flows.id"],
            name=op.f("fk_flow_versions_flow_id_flows"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_flow_versions_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_flow_versions_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_flow_versions")),
        sa.UniqueConstraint("public_id", name=op.f("uq_flow_versions_public_id")),
        sa.UniqueConstraint(
            "flow_id", "version_number", name="uq_flow_versions_flow_version"
        ),
    )
    op.create_index(
        "ix_flow_versions_flow_id_version",
        "flow_versions",
        ["flow_id", "version_number"],
        unique=False,
    )

    # RLS: mismo patrón que el resto de tablas tenant (org + visibilidad de proyecto).
    op.execute("ALTER TABLE flow_versions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE flow_versions FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY org_isolation ON flow_versions "
        "USING (org_id = current_setting('app.current_org', true)::uuid)"
    )
    op.execute(
        "CREATE POLICY project_visibility ON flow_versions "
        "USING (project_id = ANY("
        "    string_to_array("
        "        current_setting('app.allowed_projects', true), ','"
        "    )::uuid[]"
        "))"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS project_visibility ON flow_versions")
    op.execute("DROP POLICY IF EXISTS org_isolation ON flow_versions")
    op.drop_index("ix_flow_versions_flow_id_version", table_name="flow_versions")
    op.drop_table("flow_versions")
