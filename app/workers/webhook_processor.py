"""process_webhook_event Celery task (EQUIP-94).

Driven by `POST /webhooks/{platform}/{project_public_id}`. The receiver has
already verified HMAC and persisted the raw payload as one `webhook_events`
row in `pending` status.

Steps per event:
  1. Load the webhook_events row + payload.
  2. Pick the platform parser (wati/respondio/...).
  3. Resolve a target Agent via the project (first agent for now —
     multi-agent routing lands in EQUIP-109).
  4. `upsert_conversation_idempotent` to land the conversation row.
  5. Insert the message(s) parsed from the payload.
  6. If the platform marks the chat as ended, transition the conversation to
     `completed` (single SQL update with RETURNING dedups race with the Beat
     scheduler — see ADR 0002) and enqueue `evaluate_conversation`.
  7. Mark the webhook_events row as `processed` + set `processed_at`.

Failures mark the event row `failed` with `error_message` and re-raise so
Celery's retry policy applies.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session_factory
from app.models import Agent, Conversation, Message, WebhookEvent
from app.services.celery_app import celery_app
from app.services.idempotency import upsert_conversation_idempotent

logger = logging.getLogger(__name__)


def _is_terminal_payload(platform: str, payload: dict) -> bool:
    """Maps platform-specific 'chat ended' shapes to a single bool. ADR 0002."""
    status = payload.get("status") or payload.get("event_type") or ""
    if isinstance(status, str):
        return status.lower() in {"ended", "closed", "resolved", "completed"}
    return False


def _extract_messages(platform: str, payload: dict) -> tuple[str, list[dict]]:
    """Returns (external_chat_id, [{role, content, timestamp}]).

    Best-effort across WATI / Respond.io shapes. The full parser library lives
    in `app/workers/parsers/` (EQUIP-62) for CSV; webhooks for now use a
    minimal extractor that we can swap later for the same parser interface
    once schema-foundation generalises it.
    """
    chat_id = (
        payload.get("chat_id")
        or payload.get("conversationId")
        or payload.get("from")
        or payload.get("contact_id")
        or "unknown"
    )
    role = "user" if payload.get("direction", "inbound") == "inbound" else "assistant"
    content = (
        payload.get("text")
        or payload.get("body")
        or payload.get("message", {}).get("text", "")
        if isinstance(payload.get("message"), dict)
        else payload.get("text") or payload.get("body") or ""
    )
    ts_raw = payload.get("timestamp") or payload.get("created_at")
    try:
        ts = (
            datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            if isinstance(ts_raw, str)
            else datetime.now(timezone.utc)
        )
    except ValueError:
        ts = datetime.now(timezone.utc)

    return str(chat_id), [
        {
            "role": role,
            "content": str(content),
            "timestamp": ts,
        }
    ]


async def _pick_agent_id(
    session: AsyncSession, project_id: uuid.UUID
) -> uuid.UUID | None:
    """Returns the first agent in the project. Multi-agent routing pending."""
    result = await session.execute(
        select(Agent.id).where(Agent.project_id == project_id).limit(1)
    )
    return result.scalar_one_or_none()


async def _process(event_id: uuid.UUID) -> dict:
    async with async_session_factory() as session:
        ev = await session.get(WebhookEvent, event_id)
        if ev is None:
            logger.warning("[WEBHOOK_PROC] Event %s vanished", event_id)
            return {"event_id": str(event_id), "skipped": True}
        if ev.status == "processed":
            return {"event_id": str(event_id), "already_processed": True}

        agent_id = await _pick_agent_id(session, ev.project_id)
        if agent_id is None:
            ev.status = "failed"
            ev.error_message = (
                "no agent in project — create one via POST /api/v1/agents"
            )
            ev.processed_at = datetime.now(timezone.utc)
            await session.commit()
            logger.warning(
                "[WEBHOOK_PROC] No agent for project=%s event=%s",
                ev.project_id,
                event_id,
            )
            return {"event_id": str(event_id), "failed": True, "reason": "no_agent"}

        external_id, messages = _extract_messages(ev.platform, ev.payload)
        conv, was_new = await upsert_conversation_idempotent(
            session,
            agent_id=agent_id,
            external_id=external_id,
            project_id=ev.project_id,
            org_id=ev.org_id,
            platform=ev.platform,
            public_id=f"conv_{uuid.uuid4().hex[:16]}",
        )

        for m in messages:
            session.add(
                Message(
                    public_id=f"msg_{uuid.uuid4().hex[:16]}",
                    conversation_id=conv.id,
                    project_id=ev.project_id,
                    org_id=ev.org_id,
                    role=m["role"],
                    content=m["content"],
                    timestamp=m["timestamp"],
                )
            )

        terminal = _is_terminal_payload(ev.platform, ev.payload)
        if terminal:
            close = await session.execute(
                update(Conversation)
                .where(Conversation.id == conv.id, Conversation.status == "active")
                .values(status="completed", ended_at=datetime.now(timezone.utc))
                .returning(Conversation.id)
            )
            closed_row = close.scalar_one_or_none()
            if closed_row is not None:
                celery_app.send_task(
                    "app.workers.evaluator.evaluate_conversation",
                    args=[str(conv.id)],
                )
                logger.info(
                    "[WEBHOOK_PROC] Closed conversation=%s on terminal payload",
                    conv.id,
                )

        ev.status = "processed"
        ev.processed_at = datetime.now(timezone.utc)
        await session.commit()

    logger.info(
        "[WEBHOOK_PROC] event=%s conv_was_new=%s messages=%d terminal=%s",
        event_id,
        was_new,
        len(messages),
        terminal,
    )
    return {
        "event_id": str(event_id),
        "conversation_id": str(conv.id),
        "was_new_conversation": was_new,
        "messages_persisted": len(messages),
        "terminal": terminal,
    }


@celery_app.task(name="app.workers.webhook_processor.process_webhook_event", bind=True)
def process_webhook_event(self, event_id: str) -> dict:
    return asyncio.run(_process(uuid.UUID(event_id)))
