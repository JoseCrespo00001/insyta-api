"""Idempotent conversation upsert.

Webhook providers (WATI, Respond.io) retry on 5xx and sometimes on transient
network errors. Without idempotency a single inbound message turns into N
conversation rows and N Haiku evaluations — duplicated cost and corrupted data.

The unique constraint `uq_conversations_agent_id_external_id` is what makes
ON CONFLICT possible. The function returns `(row, was_new)` so the caller can
decide whether to enqueue downstream side-effects (eval, anonymizer) only on
the first insert.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation

logger = logging.getLogger(__name__)


async def upsert_conversation_idempotent(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID,
    external_id: str,
    project_id: uuid.UUID,
    org_id: uuid.UUID,
    platform: str,
    public_id: str,
    started_at: datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> tuple[Conversation, bool]:
    """Insert a conversation if `(agent_id, external_id)` is new, else fetch existing.

    Uses `INSERT ... ON CONFLICT DO NOTHING RETURNING id`. If the conflict path
    is hit (returning_id is None), we re-select the existing row.

    Returns `(conversation, was_new)`. `was_new=True` only on the first insert.
    """
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "public_id": public_id,
        "agent_id": agent_id,
        "external_id": external_id,
        "project_id": project_id,
        "org_id": org_id,
        "platform": platform,
    }
    if started_at is not None:
        values["started_at"] = started_at

    stmt = (
        pg_insert(Conversation)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=["agent_id", "external_id"],
        )
        .returning(Conversation.id)
    )

    result = await session.execute(stmt)
    inserted_id = result.scalar_one_or_none()

    if inserted_id is not None:
        logger.info(
            "[IDEMPOTENCY] Inserted conversation agent_id=%s external_id=%s",
            agent_id,
            external_id,
        )
        row = await session.get(Conversation, inserted_id)
        assert row is not None  # just inserted
        return row, True

    existing = await session.execute(
        select(Conversation).where(
            Conversation.agent_id == agent_id,
            Conversation.external_id == external_id,
        )
    )
    row = existing.scalar_one()
    logger.info(
        "[IDEMPOTENCY] Conversation already exists agent_id=%s external_id=%s",
        agent_id,
        external_id,
    )
    return row, False


__all__ = ["upsert_conversation_idempotent"]
