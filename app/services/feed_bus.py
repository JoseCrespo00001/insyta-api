"""Redis Pub/Sub bus for the live feed (EQUIP-96).

Single-publisher helper used from `app/workers/evaluator.py` after an
Evaluation is persisted (ADR 0005). The SSE endpoint subscribes per HTTP
request via `subscribe_tenant_feed`.

Failures inside `publish_eval_event` are logged and swallowed: the evaluation
row is already committed, and ADR 0004 makes the feed best-effort.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import redis
import redis.asyncio as redis_async

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def _channel(org_id: uuid.UUID) -> str:
    return f"tenant:{org_id}:feed"


def _sync_client() -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url)


def publish_eval_event(
    *,
    org_id: uuid.UUID,
    conv_id: uuid.UUID,
    score: int | None,
    topic: str | None,
    evaluated_at: datetime | None,
    project_id: uuid.UUID | None = None,
) -> None:
    """Sync publish — safe to call from a Celery task (no event loop)."""
    payload: dict[str, Any] = {
        "conv_id": str(conv_id),
        "project_id": str(project_id) if project_id else None,
        "score": score,
        "topic": topic,
        "evaluated_at": evaluated_at.isoformat() if evaluated_at else None,
    }
    try:
        _sync_client().publish(_channel(org_id), json.dumps(payload))
    except Exception as exc:
        logger.warning(
            "[FEED_BUS] publish failed org=%s conv=%s: %s", org_id, conv_id, exc
        )


async def subscribe_tenant_feed(
    org_id: uuid.UUID,
) -> AsyncIterator[str]:
    """Async generator yielding raw JSON strings published to the tenant channel.

    The caller (FastAPI EventSourceResponse) wraps each yielded string in an
    SSE `data:` frame.
    """
    client = redis_async.Redis.from_url(get_settings().redis_url)
    pubsub = client.pubsub()
    channel = _channel(org_id)
    await pubsub.subscribe(channel)
    try:
        async for message in pubsub.listen():
            if message is None:
                continue
            if message.get("type") != "message":
                continue
            data = message.get("data")
            if isinstance(data, bytes):
                yield data.decode()
            elif isinstance(data, str):
                yield data
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()
        await client.aclose()
