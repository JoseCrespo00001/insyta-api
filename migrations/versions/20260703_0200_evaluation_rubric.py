"""evaluation_rubric: rúbrica completa en evaluations (AUD-2.1)

Aditiva: agrega el JSON de la rúbrica + campos promovidos (score_bruto/final,
confidence, has_veto, veto_flags, segment, sentiment_trajectory,
requiere_revision_humana) + índices para filtrar por segment/has_veto/score_final.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-07-03 02:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c9d0e1f2a3b4"
down_revision: str | Sequence[str] | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.add_column("evaluations", sa.Column("rubric", _JSONB, nullable=True))
    op.add_column("evaluations", sa.Column("score_bruto", sa.Integer(), nullable=True))
    op.add_column("evaluations", sa.Column("score_final", sa.Integer(), nullable=True))
    op.add_column(
        "evaluations", sa.Column("confidence", sa.Numeric(4, 3), nullable=True)
    )
    op.add_column("evaluations", sa.Column("has_veto", sa.Boolean(), nullable=True))
    op.add_column("evaluations", sa.Column("veto_flags", _JSONB, nullable=True))
    op.add_column("evaluations", sa.Column("segment", sa.String(24), nullable=True))
    op.add_column(
        "evaluations", sa.Column("sentiment_trajectory", _JSONB, nullable=True)
    )
    op.add_column(
        "evaluations",
        sa.Column("requiere_revision_humana", sa.Boolean(), nullable=True),
    )
    op.create_index(
        "ix_evaluations_project_id_segment", "evaluations", ["project_id", "segment"]
    )
    op.create_index(
        "ix_evaluations_project_id_has_veto", "evaluations", ["project_id", "has_veto"]
    )
    op.create_index(
        "ix_evaluations_project_id_score_final",
        "evaluations",
        ["project_id", "score_final"],
    )


def downgrade() -> None:
    op.drop_index("ix_evaluations_project_id_score_final", table_name="evaluations")
    op.drop_index("ix_evaluations_project_id_has_veto", table_name="evaluations")
    op.drop_index("ix_evaluations_project_id_segment", table_name="evaluations")
    for col in (
        "requiere_revision_humana",
        "sentiment_trajectory",
        "segment",
        "veto_flags",
        "has_veto",
        "confidence",
        "score_final",
        "score_bruto",
        "rubric",
    ):
        op.drop_column("evaluations", col)
