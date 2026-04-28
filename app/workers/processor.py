"""process_upload Celery task (EQUIP-62).

Driven by `POST /api/v1/uploads/csv`. Steps:
  1. Load the upload row and its CSV bytes (object storage).
  2. Pick the platform parser (wati/respondio/custom_sdk).
  3. For each parsed `ConversationDTO`:
     a. `upsert_conversation_idempotent` -> bool was_new
     b. Insert messages (skip if conv_was_new=False — already loaded earlier).
     c. If was_new: enqueue `evaluate_conversation(conv_id)`.
  4. Update upload status counters.

The CSV reading is sync (csv module is fast and parsers are pure Python). The
DB writes are async via the standard session factory. Since Celery 5.4 tasks
are sync we wrap the async logic with `asyncio.run`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session_factory
from app.models import Message
from app.services.celery_app import celery_app
from app.services.idempotency import upsert_conversation_idempotent
from app.workers.parsers import ConversationDTO, get_parser

logger = logging.getLogger(__name__)


async def _load_upload_blob(upload_id: uuid.UUID) -> tuple[bytes, str, dict]:
    """Stub: loads upload metadata + CSV bytes.

    The real implementation is in secure-backend's uploads router. For unit
    tests we monkeypatch this function.
    """
    raise NotImplementedError(
        "process_upload requires uploads router (secure-backend) to be wired"
    )


async def _persist_messages(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    project_id: uuid.UUID,
    org_id: uuid.UUID,
    dtos: Iterable,
) -> int:
    rows = []
    for dto in dtos:
        rows.append(
            {
                "id": uuid.uuid4(),
                "public_id": f"msg_{uuid.uuid4().hex[:16]}",
                "conversation_id": conversation_id,
                "project_id": project_id,
                "org_id": org_id,
                "role": dto.role,
                "content": dto.content,
                "timestamp": dto.timestamp,
            }
        )
    if not rows:
        return 0
    await session.execute(Message.__table__.insert().values(rows))
    return len(rows)


async def process_conversations(
    *,
    parsed: Iterable[ConversationDTO],
    project_id: uuid.UUID,
    org_id: uuid.UUID,
    agent_id: uuid.UUID,
    enqueue_eval,
) -> dict:
    """Bulk-process parsed DTOs. Returns counters."""
    new_count = 0
    dup_count = 0
    msg_count = 0
    for dto in parsed:
        async with async_session_factory() as session:
            conv, was_new = await upsert_conversation_idempotent(
                session,
                agent_id=agent_id,
                external_id=dto.external_id,
                project_id=project_id,
                org_id=org_id,
                platform=dto.platform,
                public_id=f"conv_{uuid.uuid4().hex[:16]}",
                started_at=dto.started_at,
            )
            if was_new:
                msg_count += await _persist_messages(
                    session,
                    conversation_id=conv.id,
                    project_id=project_id,
                    org_id=org_id,
                    dtos=dto.messages,
                )
                await session.commit()
                new_count += 1
                if enqueue_eval is not None:
                    enqueue_eval(str(conv.id))
            else:
                dup_count += 1
    return {
        "new_conversations": new_count,
        "duplicate_conversations": dup_count,
        "messages_persisted": msg_count,
    }


async def _run(upload_id: uuid.UUID) -> dict:
    csv_bytes, platform, meta = await _load_upload_blob(upload_id)
    parser = get_parser(platform)
    parsed = list(parser(csv_bytes))
    project_id = uuid.UUID(meta["project_id"])
    org_id = uuid.UUID(meta["org_id"])
    agent_id = uuid.UUID(meta["agent_id"])

    def _enqueue(conv_id: str) -> None:
        from app.workers.evaluator import evaluate_conversation

        evaluate_conversation.delay(conv_id)

    summary = await process_conversations(
        parsed=parsed,
        project_id=project_id,
        org_id=org_id,
        agent_id=agent_id,
        enqueue_eval=_enqueue,
    )
    summary["upload_id"] = str(upload_id)
    summary["parsed_conversations"] = len(parsed)
    return summary


@celery_app.task(name="app.workers.processor.process_upload", bind=True)
def process_upload(self, upload_id: str) -> dict:
    return asyncio.run(_run(uuid.UUID(upload_id)))
