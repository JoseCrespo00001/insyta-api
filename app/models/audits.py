"""Audit models — an audit run + its conversation set + per-message verdicts.

An Audit picks a Flow + a set of Conversations + emphasis/free_text, then a
worker runs the LLM-as-judge to produce a conversation-level Report (composed
from Evaluations) and a per-message report (`MessageEvaluation`).
Maps to the frontend `Audit` / `Report` types.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Audit(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "audits"

    public_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    flow_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("flows.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Objetivo de la campaña (estilo Meta): leads | ventas | awareness | ...
    objective: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Motor del judge: anthropic | deepseek
    provider: Mapped[str | None] = mapped_column(String(16), nullable=True)
    emphasis: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    free_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    conversation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    suggestions: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    report_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_audits_project_id_created_at", "project_id", "created_at"),
        Index("ix_audits_flow_id", "flow_id"),
        CheckConstraint(
            "status IN ('running','active','archived','failed')",
            name="audit_status_enum",
        ),
    )


class AuditConversation(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_conversations"

    audit_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("audits.id", ondelete="CASCADE"),
        nullable=False,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    included_in_report: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    __table_args__ = (
        UniqueConstraint(
            "audit_id", "conversation_id", name="uq_audit_conversations_audit_conv"
        ),
        Index("ix_audit_conversations_audit_id", "audit_id"),
        Index("ix_audit_conversations_conversation_id", "conversation_id"),
    )


class MessageEvaluation(UUIDPrimaryKeyMixin, Base):
    """Per-message verdict from the judge (the 'reporte por mensaje')."""

    __tablename__ = "message_evaluations"

    public_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    message_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    audit_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("audits.id", ondelete="SET NULL"),
        nullable=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    label: Mapped[str | None] = mapped_column(String(16), nullable=True)
    issue_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    issue_subtype: Mapped[str | None] = mapped_column(String(64), nullable=True)
    severity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=__import__("sqlalchemy").func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "audit_id", "message_id", name="uq_message_evaluations_audit_message"
        ),
        Index("ix_message_evaluations_conversation_id", "conversation_id"),
        Index("ix_message_evaluations_audit_id", "audit_id"),
        CheckConstraint(
            "label IS NULL OR label IN ('ok','warning','error')",
            name="message_eval_label_enum",
        ),
        CheckConstraint(
            "severity IS NULL OR severity IN ('baja','media','alta','critica')",
            name="message_eval_severity_enum",
        ),
    )
