"""SQLAlchemy ORM models.

All models inherit from a single `Base` so Alembic autogen sees them under one
metadata. Importing this package registers every model on `Base.metadata`.
"""

from __future__ import annotations

from app.models.audit import Alert, WebhookEvent
from app.models.base import Base
from app.models.tenancy import (
    Agent,
    ApiKey,
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
    "Alert",
    "ApiKey",
    "Base",
    "Conversation",
    "Evaluation",
    "Message",
    "Organization",
    "Project",
    "Upload",
    "User",
    "WebhookEvent",
]
