"""Reputación acumulada de agente y usuario (rúbrica §6, AUD-4.1).

Memoria persistente que condiciona lecturas futuras del judge:
- `agent_reputation`: media móvil del score, tasa de VETO, baseline/límites para
  SPC (Fase 6) y tendencia, por `agent_id`.
- `user_reputation`: por usuario (clave = hash del external_id) — sentimiento
  histórico, lead score, intentos de fraude, etiqueta.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


class AgentReputation(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "agent_reputation"

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
    agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    avg_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    score_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    veto_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # SPC (se completa en Fase 6).
    baseline_mean: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    baseline_std: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)
    ucl: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    lcl: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    # mejora | estable | degradacion
    trend: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (Index("ix_agent_reputation_project_id", "project_id"),)


class UserReputation(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "user_reputation"

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
    # Clave de usuario = hash (sha256 hex) del external_id (no guardamos el número crudo).
    user_key: Mapped[str] = mapped_column(String(64), nullable=False)
    avg_sentiment: Mapped[Decimal | None] = mapped_column(Numeric(4, 2), nullable=True)
    sentiment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lead_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    fraud_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    usuario_riesgoso: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # lead_calificado | recurrente | neutral | problematico
    etiqueta: Mapped[str | None] = mapped_column(String(24), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "project_id", "user_key", name="uq_user_reputation_project_user"
        ),
        Index("ix_user_reputation_project_id", "project_id"),
    )
