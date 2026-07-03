"""ORM mappings for the multi-tenant core.

RLS is enabled in the migration (not at ORM level) — these models are pure data
shape. Schema follows the Flujo + Auditoría product model that `insyta-web`
consumes (see `insyta-web/src/lib/projects/types.ts`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "organizations"

    public_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    org_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="customer"
    )
    plan: Mapped[str] = mapped_column(String(32), nullable=False, default="free")
    white_label_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # API keys de proveedores LLM cargadas desde el front, cifradas (Fernet).
    anthropic_api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    deepseek_api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "org_type IN ('customer','agency')",
            name="org_type_enum",
        ),
        CheckConstraint(
            "plan IN ('free','starter','growth','business','agency','enterprise')",
            name="plan_enum",
        ),
    )


class Project(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "projects"

    public_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    environment: Mapped[str] = mapped_column(String(8), nullable=False, default="live")
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    alert_thresholds: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Datos de la empresa (contexto del negocio) que el judge usa al auditar.
    company_context: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("org_id", "slug", name="uq_projects_org_id_slug"),
        Index("ix_projects_org_id_created_at", "org_id", "created_at"),
        CheckConstraint("environment IN ('live','test')", name="environment_enum"),
    )


class Agent(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "agents"

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
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("project_id", "slug", name="uq_agents_project_id_slug"),
        Index("ix_agents_project_id_created_at", "project_id", "created_at"),
        CheckConstraint(
            "platform IN ('wati','respondio','manychat','twilio','custom_sdk')",
            name="platform_enum",
        ),
    )


class Conversation(UUIDPrimaryKeyMixin, SoftDeleteMixin, Base):
    __tablename__ = "conversations"

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
    agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    upload_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("uploads.id", ondelete="SET NULL"),
        nullable=True,
    )
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    contact_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    phoenix_trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=__import__("sqlalchemy").func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "agent_id", "external_id", name="uq_conversations_agent_id_external_id"
        ),
        Index("ix_conversations_project_id_created_at", "project_id", "created_at"),
        Index("ix_conversations_upload_id", "upload_id"),
        Index(
            "ix_conversations_project_id_agent_id_created_at",
            "project_id",
            "agent_id",
            "created_at",
        ),
        CheckConstraint(
            "status IN ('active','completed','abandoned','escalated')",
            name="status_enum",
        ),
    )


class Message(UUIDPrimaryKeyMixin, SoftDeleteMixin, Base):
    __tablename__ = "messages"

    public_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
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
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_anonymized: Mapped[str | None] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    extra: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=__import__("sqlalchemy").func.now(),
    )

    __table_args__ = (
        Index("ix_messages_conversation_id_seq", "conversation_id", "seq"),
        CheckConstraint("role IN ('user','assistant','system')", name="role_enum"),
    )


class Evaluation(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "evaluations"

    public_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
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
    agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolution: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    satisfaction: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tone: Mapped[str | None] = mapped_column(String(16), nullable=True)
    frustration: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    escalated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    efficiency: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scope_violation: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    topic: Mapped[str | None] = mapped_column(String(128), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_used: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tokens_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_input: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_output: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    phoenix_trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phoenix_span_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_evaluations_project_id_evaluated_at", "project_id", "evaluated_at"),
        Index("ix_evaluations_agent_id_topic", "agent_id", "topic"),
        Index("ix_evaluations_project_id_score", "project_id", "score"),
        CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 100)",
            name="score_range",
        ),
        CheckConstraint(
            "satisfaction IS NULL OR (satisfaction >= 1 AND satisfaction <= 5)",
            name="satisfaction_range",
        ),
        CheckConstraint(
            "efficiency IS NULL OR (efficiency >= 1 AND efficiency <= 5)",
            name="efficiency_range",
        ),
        CheckConstraint(
            "tone IS NULL OR tone IN ('positive','neutral','negative')",
            name="tone_enum",
        ),
    )


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    public_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    supabase_user_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="owner")
    allowed_project_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "role IN ('owner','admin','editor','member','viewer')",
            name="user_role_enum",
        ),
        Index("ix_users_org_id", "org_id"),
    )


# Relationships are kept light to avoid implicit eager loading; downstream
# services explicitly join via SQL where needed.
Organization.projects = relationship(  # type: ignore[attr-defined]
    "Project", backref="organization", cascade="all, delete-orphan"
)
Project.agents = relationship(  # type: ignore[attr-defined]
    "Agent", backref="project", cascade="all, delete-orphan"
)
Agent.conversations = relationship(  # type: ignore[attr-defined]
    "Conversation", backref="agent", cascade="all, delete-orphan"
)
Conversation.messages = relationship(  # type: ignore[attr-defined]
    "Message", backref="conversation", cascade="all, delete-orphan"
)
Conversation.evaluation = relationship(  # type: ignore[attr-defined]
    "Evaluation", backref="conversation", uselist=False, cascade="all, delete-orphan"
)
