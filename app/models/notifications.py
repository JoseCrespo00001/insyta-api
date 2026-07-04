"""Notification: feed real de eventos para la campana del dashboard.

Reemplaza el mock del front (`lib/notifications.ts`). Las emite el worker de
auditoría (auditoría completada, conversación crítica, sugerencias nuevas). RLS
multi-tenant + soft-delete como el resto. `event_key` deduplica: re-correr una
auditoría no crea notificaciones repetidas (unique (org_id, event_key)).
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Notification(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "notifications"

    public_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Proyecto asociado (nullable: puede haber notificaciones a nivel org).
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
    )
    # audit | suspicious | improvement | update
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Deep-link opcional en la app (ej. /projects/{public_id}).
    link: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Estado leído a nivel org (los orgs son mono-usuario hoy; per-user = futuro).
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Clave de deduplicación por evento (ej. "audit_completed:<audit_id>").
    event_key: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        UniqueConstraint("org_id", "event_key", name="uq_notifications_org_event"),
        Index("ix_notifications_org_id_created_at", "org_id", "created_at"),
        Index("ix_notifications_org_id_is_read", "org_id", "is_read"),
        Index("ix_notifications_is_deleted", "is_deleted"),
    )
