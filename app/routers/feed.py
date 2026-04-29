"""GET /api/v1/feed/stream — Server-Sent Events live feed (EQUIP-96).

Subscribes one Redis Pub/Sub channel per HTTP connection (`tenant:{org_id}:feed`).
Each evaluation publish (see `app/workers/evaluator.py`) hits every connected
client of that org. ADR 0004 keeps the channel stateless: reconnects do not
backfill — clients reconcile via REST.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sse_starlette.sse import EventSourceResponse

from app.core.auth import CurrentUser, get_current_user
from app.services.feed_bus import subscribe_tenant_feed

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["feed"])


@router.get("/feed/stream")
async def feed_stream(
    current_user: CurrentUser = Depends(get_current_user),
) -> EventSourceResponse:
    async def event_generator():
        logger.info(
            "[SSE] subscribing org=%s user=%s",
            current_user.org_id,
            current_user.user_id,
        )
        async for raw in subscribe_tenant_feed(current_user.org_id):
            yield {"event": "evaluation", "data": raw}

    return EventSourceResponse(event_generator())
