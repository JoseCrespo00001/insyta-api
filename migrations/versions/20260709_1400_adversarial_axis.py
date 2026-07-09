"""adversarial_axis: eje adversarial en evaluations (Prompt 3/4)

Aditiva y prod-safe (no destructiva):
- is_adversarial BOOLEAN NOT NULL default false  → backfill trivial a false.
- attack_type VARCHAR(32) NULL + CHECK del enum.
- attack_repelled BOOLEAN NULL.
- veto_confidence NUMERIC(4,3) NULL.
- veto_firm BOOLEAN NOT NULL default false  → se PROMUEVE la key `veto_firm` que
  hasta ahora vivía dentro del JSON `rubric` (B7). Backfill que COPIA el valor
  existente del JSONB a la columna (preserva data, no la borra).
- Índice (project_id, is_adversarial) para la capa de riesgo.

Los NOT NULL usan server_default → las filas existentes se rellenan sin bloqueo
destructivo. Los backfills solo POBLAN columnas nuevas desde datos ya presentes.

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-07-09 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4c5d6e7f8a9"
down_revision: str | Sequence[str] | None = "a3b4c5d6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ATTACK_TYPES = (
    "jailbreak",
    "prompt_injection",
    "manipulacion_legal",
    "manipulacion_precio",
    "otro",
)


def upgrade() -> None:
    op.add_column(
        "evaluations",
        sa.Column(
            "is_adversarial",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column("evaluations", sa.Column("attack_type", sa.String(32), nullable=True))
    op.add_column("evaluations", sa.Column("attack_repelled", sa.Boolean(), nullable=True))
    op.add_column("evaluations", sa.Column("veto_confidence", sa.Numeric(4, 3), nullable=True))
    op.add_column(
        "evaluations",
        sa.Column("veto_firm", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    _enum = ",".join(f"'{t}'" for t in _ATTACK_TYPES)
    op.create_check_constraint(
        "attack_type_enum",
        "evaluations",
        f"attack_type IS NULL OR attack_type IN ({_enum})",
    )
    op.create_index(
        "ix_evaluations_project_id_is_adversarial",
        "evaluations",
        ["project_id", "is_adversarial"],
    )
    # Backfill NO destructivo: promover veto_firm del JSONB `rubric` a la columna
    # (preserva la firmeza de VETO ya computada en evals previas), y veto_confidence
    # desde el `confidence` general cuando la conversación tenía veto.
    op.execute(
        sa.text(
            "UPDATE evaluations SET veto_firm = TRUE "
            "WHERE rubric IS NOT NULL AND (rubric->>'veto_firm') = 'true'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE evaluations SET veto_confidence = confidence "
            "WHERE has_veto = TRUE AND confidence IS NOT NULL"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_evaluations_project_id_is_adversarial", table_name="evaluations")
    op.drop_constraint("attack_type_enum", "evaluations", type_="check")
    for col in (
        "veto_firm",
        "veto_confidence",
        "attack_repelled",
        "attack_type",
        "is_adversarial",
    ):
        op.drop_column("evaluations", col)
