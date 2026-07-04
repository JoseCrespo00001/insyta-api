"""Supervisor: cerebro reusable de auditoría.

Bundlea lo que hoy se re-ingresa por auditoría: el flujo a auditar + la base de
conocimiento + la data adjunta (fuente de verdad: precios.json / info.json) + los
defaults de objetivo/énfasis. Al crear una auditoría se elige un Supervisor y la
auditoría hereda todo esto (con override opcional).
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Supervisor(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "supervisors"

    public_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Flujo a auditar (opcional: se puede auditar solo con knowledge/attached_data).
    flow_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("flows.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Base de conocimiento (datos de la empresa/proyecto) que hoy vive en
    # projects.company_context; acá pasa a ser parte del supervisor.
    knowledge_base: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Data adjunta = fuente de verdad para el judge. dict de JSONs nombrados:
    # {"precios": {...}, "info": {...}, ...}. El judge la usa para NO marcar como
    # alucinación lo que coincide con estos datos.
    attached_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Defaults que la auditoría hereda.
    default_objective: Mapped[str | None] = mapped_column(String(32), nullable=True)
    default_emphasis: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    default_free_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_supervisors_project_id_created_at", "project_id", "created_at"),
        Index("ix_supervisors_flow_id", "flow_id"),
    )
