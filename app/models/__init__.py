"""SQLAlchemy ORM models.

All models inherit from a single `Base` so Alembic autogen sees them under one
metadata. Importing this package registers every model on `Base.metadata`.
"""

from __future__ import annotations

from app.models.audits import Audit, AuditConversation, MessageEvaluation
from app.models.base import Base
from app.models.flow_versions import FlowVersion
from app.models.flows import Flow
from app.models.improvements import Improvement, ImprovementConversation
from app.models.supervisors import Supervisor
from app.models.tenancy import (
    Agent,
    Conversation,
    Evaluation,
    Message,
    Organization,
    Project,
    User,
)
from app.models.uploads import Upload

__all__ = [
    "Agent",
    "Audit",
    "AuditConversation",
    "Base",
    "Conversation",
    "Evaluation",
    "Flow",
    "FlowVersion",
    "Improvement",
    "ImprovementConversation",
    "Message",
    "MessageEvaluation",
    "Organization",
    "Project",
    "Supervisor",
    "Upload",
    "User",
]
