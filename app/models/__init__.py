"""SQLAlchemy ORM models.

All models inherit from a single `Base` so Alembic autogen sees them under one
metadata. Importing this package registers every model on `Base.metadata`.
"""

from __future__ import annotations

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

__all__ = [
    "Agent",
    "ApiKey",
    "Base",
    "Conversation",
    "Evaluation",
    "Message",
    "Organization",
    "Project",
    "User",
]
