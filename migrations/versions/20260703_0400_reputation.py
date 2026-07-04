"""reputation: agent_reputation + user_reputation con RLS (AUD-4.1)

Memoria de reputación por agente y por usuario (hash del external_id). RLS
multi-tenant + soft-delete. Aditiva.

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-07-03 04:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: str | Sequence[str] | None = "d0e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _common_cols() -> list[sa.Column]:
    return [
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
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
    ]


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY org_isolation ON {table} "
        "USING (org_id = current_setting('app.current_org', true)::uuid)"
    )
    op.execute(
        f"CREATE POLICY project_visibility ON {table} "
        "USING (project_id = ANY(string_to_array("
        "current_setting('app.allowed_projects', true), ',')::uuid[]))"
    )


def upgrade() -> None:
    op.create_table(
        "agent_reputation",
        *_common_cols(),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("avg_score", sa.Numeric(5, 2), nullable=True),
        sa.Column("score_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("veto_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("baseline_mean", sa.Numeric(5, 2), nullable=True),
        sa.Column("baseline_std", sa.Numeric(6, 3), nullable=True),
        sa.Column("ucl", sa.Numeric(5, 2), nullable=True),
        sa.Column("lcl", sa.Numeric(5, 2), nullable=True),
        sa.Column("trend", sa.String(16), nullable=True),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_reputation")),
        sa.UniqueConstraint("public_id", name=op.f("uq_agent_reputation_public_id")),
        sa.UniqueConstraint("agent_id", name=op.f("uq_agent_reputation_agent_id")),
    )
    op.create_index(
        "ix_agent_reputation_project_id", "agent_reputation", ["project_id"]
    )
    op.create_index(
        "ix_agent_reputation_is_deleted", "agent_reputation", ["is_deleted"]
    )
    _enable_rls("agent_reputation")

    op.create_table(
        "user_reputation",
        *_common_cols(),
        sa.Column("user_key", sa.String(64), nullable=False),
        sa.Column("avg_sentiment", sa.Numeric(4, 2), nullable=True),
        sa.Column("sentiment_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lead_score", sa.Numeric(5, 2), nullable=True),
        sa.Column("fraud_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "usuario_riesgoso", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("etiqueta", sa.String(24), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_reputation")),
        sa.UniqueConstraint("public_id", name=op.f("uq_user_reputation_public_id")),
        sa.UniqueConstraint(
            "project_id", "user_key", name="uq_user_reputation_project_user"
        ),
    )
    op.create_index("ix_user_reputation_project_id", "user_reputation", ["project_id"])
    op.create_index("ix_user_reputation_is_deleted", "user_reputation", ["is_deleted"])
    _enable_rls("user_reputation")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS project_visibility ON user_reputation")
    op.execute("DROP POLICY IF EXISTS org_isolation ON user_reputation")
    op.drop_index("ix_user_reputation_is_deleted", table_name="user_reputation")
    op.drop_index("ix_user_reputation_project_id", table_name="user_reputation")
    op.drop_table("user_reputation")
    op.execute("DROP POLICY IF EXISTS project_visibility ON agent_reputation")
    op.execute("DROP POLICY IF EXISTS org_isolation ON agent_reputation")
    op.drop_index("ix_agent_reputation_is_deleted", table_name="agent_reputation")
    op.drop_index("ix_agent_reputation_project_id", table_name="agent_reputation")
    op.drop_table("agent_reputation")
