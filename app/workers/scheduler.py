"""Celery Beat scheduler tasks (EQUIP-102).

`close_stale_conversations` runs every 5 minutes (configured in
`celery_app.conf.beat_schedule`). It walks all `conversations` with
`status='active'` whose most recent message is older than the agent's
`completion_idle_minutes` (default 30) and:
  1. Sets status='completed' and ended_at=NOW().
  2. Enqueues `evaluate_conversation(conv_id)`.

The query joins messages to find the latest timestamp per conversation. To
keep the SQL fast we rely on the `(conversation_id, timestamp)` index added
in migration 0001.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text, update

from app.core.db import async_session_factory
from app.models import Agent, Conversation
from app.services.celery_app import celery_app

logger = logging.getLogger(__name__)

DEFAULT_IDLE_MINUTES = 30


def _idle_minutes_for(agent: Agent) -> int:
    cfg: dict[str, Any] = agent.config or {}
    raw = cfg.get("completion_idle_minutes", DEFAULT_IDLE_MINUTES)
    try:
        v = int(raw)
        return max(1, v)
    except (TypeError, ValueError):
        return DEFAULT_IDLE_MINUTES


async def _find_stale_ids() -> list[tuple[str, int]]:
    """Returns (conversation_id, idle_minutes) for each stale conversation.

    We compute the cutoff per-agent so each tenant's threshold is honored.
    The query is one SELECT per agent — fine for Fase 1 (10s of agents).
    Optimize with a windowed CTE if we ever exceed ~1k agents.
    """
    out: list[tuple[str, int]] = []
    async with async_session_factory() as session:
        agents = (await session.execute(select(Agent))).scalars().all()
        for agent in agents:
            idle_min = _idle_minutes_for(agent)
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=idle_min)
            rows = await session.execute(
                text(
                    """
                    SELECT c.id::text
                    FROM conversations c
                    LEFT JOIN LATERAL (
                        SELECT MAX(m.timestamp) AS last_ts
                        FROM messages m
                        WHERE m.conversation_id = c.id
                    ) lm ON true
                    WHERE c.agent_id = :agent_id
                      AND c.status = 'active'
                      AND COALESCE(lm.last_ts, c.started_at, c.created_at) < :cutoff
                    """
                ),
                {"agent_id": agent.id, "cutoff": cutoff},
            )
            for (conv_id,) in rows.all():
                out.append((conv_id, idle_min))
    return out


async def _mark_and_enqueue(conv_id: str) -> None:
    async with async_session_factory() as session:
        await session.execute(
            update(Conversation)
            .where(Conversation.id == conv_id)
            .where(Conversation.status == "active")
            .values(status="completed", ended_at=datetime.now(timezone.utc))
        )
        await session.commit()
    # Lazy import to avoid Celery circular import at module load.
    from app.workers.evaluator import evaluate_conversation

    evaluate_conversation.delay(conv_id)


async def _run() -> dict:
    stale = await _find_stale_ids()
    closed = 0
    for conv_id, _idle in stale:
        try:
            await _mark_and_enqueue(conv_id)
            closed += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("[SCHEDULER] failed to close conv=%s: %s", conv_id, exc)
    if stale:
        logger.info("[SCHEDULER] closed %d/%d stale conversations", closed, len(stale))
    return {"checked": len(stale), "closed": closed}


@celery_app.task(name="app.workers.scheduler.close_stale_conversations")
def close_stale_conversations() -> dict:
    return asyncio.run(_run())
