"""supervisors: cerebro reusable de auditoría + audits.supervisor_id

Nueva tabla `supervisors` (flow + knowledge_base + attached_data + defaults) con
RLS multi-tenant + soft-delete, y FK opcional `audits.supervisor_id`. Aditiva.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-07-03 01:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b8c9d0e1f2a3"
down_revision: str | Sequence[str] | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "supervisors",
        sa.Column("public_id", sa.String(length=64), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("flow_id", sa.UUID(), nullable=True),
        sa.Column("knowledge_base", sa.Text(), nullable=True),
        sa.Column(
            "attached_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("default_objective", sa.String(length=32), nullable=True),
        sa.Column(
            "default_emphasis", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("default_free_text", sa.Text(), nullable=True),
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
            "is_deleted",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_supervisors_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_supervisors_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["flow_id"],
            ["flows.id"],
            name=op.f("fk_supervisors_flow_id_flows"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_supervisors")),
        sa.UniqueConstraint("public_id", name=op.f("uq_supervisors_public_id")),
    )
    op.create_index(
        "ix_supervisors_project_id_created_at",
        "supervisors",
        ["project_id", "created_at"],
    )
    op.create_index("ix_supervisors_flow_id", "supervisors", ["flow_id"])
    op.create_index("ix_supervisors_is_deleted", "supervisors", ["is_deleted"])

    # RLS: mismo patrón tenant que el resto de tablas.
    op.execute("ALTER TABLE supervisors ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE supervisors FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY org_isolation ON supervisors "
        "USING (org_id = current_setting('app.current_org', true)::uuid)"
    )
    op.execute(
        "CREATE POLICY project_visibility ON supervisors "
        "USING (project_id = ANY("
        "    string_to_array("
        "        current_setting('app.allowed_projects', true), ','"
        "    )::uuid[]"
        "))"
    )

    # audits.supervisor_id (FK opcional, SET NULL).
    op.add_column("audits", sa.Column("supervisor_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_audits_supervisor_id_supervisors"),
        "audits",
        "supervisors",
        ["supervisor_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_audits_supervisor_id_supervisors"), "audits", type_="foreignkey"
    )
    op.drop_column("audits", "supervisor_id")
    op.execute("DROP POLICY IF EXISTS project_visibility ON supervisors")
    op.execute("DROP POLICY IF EXISTS org_isolation ON supervisors")
    op.drop_index("ix_supervisors_is_deleted", table_name="supervisors")
    op.drop_index("ix_supervisors_flow_id", table_name="supervisors")
    op.drop_index("ix_supervisors_project_id_created_at", table_name="supervisors")
    op.drop_table("supervisors")
