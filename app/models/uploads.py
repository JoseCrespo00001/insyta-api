"""Upload tracking table.

Each row represents a CSV upload submitted via POST /api/v1/uploads/csv. The
worker (`app.workers.processor.process_upload`) updates `status` as it parses,
anonymizes and persists conversations.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Upload(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "uploads"

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
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=True,
    )
    filename: Mapped[str] = mapped_column(String(256), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    rows_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rows_processed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','processing','completed','failed')",
            name="status_enum",
        ),
        Index("ix_uploads_project_id_created_at", "project_id", "created_at"),
        Index("ix_uploads_org_id_status", "org_id", "status"),
    )
