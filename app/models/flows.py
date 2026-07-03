"""Flow model — the agent's expected conversational "brain".

A Flow stores a Langflow export (nodes + edges) as `flow_json` plus a light
`flow_metadata` blob for fast listing/visualisation without loading the whole
graph. Maps to the frontend `Flujo` type (`insyta-web/src/lib/projects/types.ts`).
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Flow(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "flows"

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
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0")
    flow_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    flow_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    agent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        Index("ix_flows_project_id_created_at", "project_id", "created_at"),
        Index("ix_flows_project_id_is_active", "project_id", "is_active"),
    )
