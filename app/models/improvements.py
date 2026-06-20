"""Improvement models — a per-flow improvement proposal + affected conversations.

An Improvement is a suggested change to *add to / improve* a Flow, surfaced
next to the flow. Produced from an Audit. Maps to the frontend
`FlujoImprovement` type ({title, detail, impact, why, conversations[]}).
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Improvement(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "improvements"

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
    flow_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("flows.id", ondelete="CASCADE"),
        nullable=False,
    )
    audit_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("audits.id", ondelete="SET NULL"),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    impact: Mapped[str] = mapped_column(String(200), nullable=False)
    why: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")

    __table_args__ = (
        Index("ix_improvements_project_id_created_at", "project_id", "created_at"),
        Index("ix_improvements_flow_id", "flow_id"),
        CheckConstraint(
            "status IN ('pending','approved','rejected','applied','measured')",
            name="improvement_status_enum",
        ),
    )


class ImprovementConversation(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "improvement_conversations"

    improvement_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("improvements.id", ondelete="CASCADE"),
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

    __table_args__ = (
        UniqueConstraint(
            "improvement_id",
            "conversation_id",
            name="uq_improvement_conversations_imp_conv",
        ),
        Index("ix_improvement_conversations_improvement_id", "improvement_id"),
    )
