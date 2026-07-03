"""FlowVersion model — immutable snapshots of a Flow's `flow_json` over time.

Every time a Flow's JSON changes (initial upload, an applied improvement, a
manual edit or a restore) we keep a snapshot here so the user can browse the
history and roll back to any past version. The *current* version is always the
one with the highest `version_number` (a restore creates a new snapshot rather
than mutating history). Per-flow numbering; a Flow can have many versions and a
Project many flows.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

# initial=primera carga · improvement=mejora aplicada · manual=edición a mano
# · restore=se volvió a una versión pasada · upload=re-subida del JSON
FLOW_VERSION_SOURCES = ("initial", "improvement", "manual", "restore", "upload")


class FlowVersion(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "flow_versions"

    public_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    flow_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("flows.id", ondelete="CASCADE"),
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
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    flow_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    agent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint(
            "flow_id", "version_number", name="uq_flow_versions_flow_version"
        ),
        Index("ix_flow_versions_flow_id_version", "flow_id", "version_number"),
        CheckConstraint(
            "source IN ('initial','improvement','manual','restore','upload')",
            name="ck_flow_versions_source_enum",
        ),
    )
