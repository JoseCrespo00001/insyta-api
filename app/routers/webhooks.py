"""Inbound webhook receivers (WATI, Respond.io).

Handler responsibilities (target <1 s):
1. Verify HMAC against the project's Fernet-decrypted secret.
2. Compute a stable idempotency_key and persist one `webhook_events` row.
3. Enqueue `app.workers.webhook_processor.process_webhook_event(event_id)` on
   Celery — by name so this module does not depend on the worker code path.
4. Return 200 with `{received, platform}`.

A duplicate inbound (same idempotency_key) is a no-op insert; we still 200
because the provider's correct behavior is to keep retrying on non-2xx.

See ADR 0001 for the row-per-event rationale.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models import Project, WebhookEvent
from app.services.celery_app import celery_app
from app.services.rate_limit import WEBHOOK_RATE_LIMIT, limiter
from app.services.webhook_secret import decrypt_webhook_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

SIGNATURE_PREFIX = "sha256="


class WebhookAck(BaseModel):
    received: bool
    platform: str


async def _load_project(
    session: AsyncSession, project_public_id: str
) -> tuple[uuid.UUID, uuid.UUID, str]:
    result = await session.execute(
        select(Project.id, Project.org_id, Project.webhook_secret_encrypted).where(
            Project.public_id == project_public_id
        )
    )
    row = result.one_or_none()
    if row is None:
        logger.warning(
            "[WEBHOOK] Unknown project public_id=%s — rejecting", project_public_id
        )
        raise HTTPException(status_code=401, detail="Invalid signature")
    try:
        secret = decrypt_webhook_secret(bytes(row.webhook_secret_encrypted))
    except RuntimeError:
        logger.error(
            "[WEBHOOK] Could not decrypt secret for project=%s", project_public_id
        )
        raise HTTPException(status_code=500, detail="Server misconfigured") from None
    return row.id, row.org_id, secret


def _verify_signature(secret: str, body: bytes, header_value: str) -> bool:
    if not header_value.startswith(SIGNATURE_PREFIX):
        return False
    provided = header_value[len(SIGNATURE_PREFIX) :]
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided, expected)


def _idempotency_key(platform: str, payload: dict[str, Any]) -> str:
    """Stable per-event key. Uses message_id when the platform supplies it,
    otherwise falls back to a payload hash so retries of the same body dedup.
    """
    msg_id = (
        payload.get("message_id") or payload.get("messageId") or payload.get("id") or ""
    )
    fallback = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    raw = f"{platform}:{msg_id}:{fallback}".encode()
    return hashlib.sha256(raw).hexdigest()


async def _persist_and_enqueue(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    org_id: uuid.UUID,
    platform: str,
    payload: dict[str, Any],
) -> tuple[uuid.UUID | None, bool]:
    """Returns (event_id, was_new). Idempotent under (project_id, idempotency_key)."""
    idem = _idempotency_key(platform, payload)
    event_id = uuid.uuid4()
    stmt = (
        pg_insert(WebhookEvent)
        .values(
            id=event_id,
            project_id=project_id,
            org_id=org_id,
            platform=platform,
            payload=payload,
            signature_verified=True,
            idempotency_key=idem,
            status="pending",
            received_at=datetime.now(timezone.utc),
        )
        .on_conflict_do_nothing(
            index_elements=["project_id", "idempotency_key"],
        )
        .returning(WebhookEvent.id)
    )
    result = await session.execute(stmt)
    inserted = result.scalar_one_or_none()
    await session.commit()
    if inserted is None:
        logger.info(
            "[WEBHOOK] Duplicate event project=%s idem=%s — skipping enqueue",
            project_id,
            idem[:12],
        )
        return None, False
    celery_app.send_task(
        "app.workers.webhook_processor.process_webhook_event",
        args=[str(inserted)],
    )
    logger.info(
        "[WEBHOOK] Persisted+enqueued project=%s platform=%s event=%s",
        project_id,
        platform,
        inserted,
    )
    return inserted, True


async def _handle_inbound(
    *,
    platform: str,
    project_public_id: str,
    request: Request,
    signature: str | None,
    session: AsyncSession,
) -> WebhookAck:
    if not signature:
        logger.info("[WEBHOOK_%s] Missing signature header", platform.upper())
        raise HTTPException(status_code=401, detail="Missing signature header")

    body = await request.body()
    project_id, org_id, secret = await _load_project(session, project_public_id)

    if not _verify_signature(secret, body, signature):
        logger.warning(
            "[WEBHOOK_%s] Invalid signature for project=%s",
            platform.upper(),
            project_public_id,
        )
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload: dict[str, Any] = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    await _persist_and_enqueue(
        session,
        project_id=project_id,
        org_id=org_id,
        platform=platform,
        payload=payload,
    )
    return WebhookAck(received=True, platform=platform)


@router.post(
    "/wati/{project_public_id}",
    response_model=WebhookAck,
    status_code=status.HTTP_200_OK,
)
@limiter.limit(WEBHOOK_RATE_LIMIT)
async def wati_webhook(
    project_public_id: str,
    request: Request,
    x_wati_signature: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db),
) -> WebhookAck:
    return await _handle_inbound(
        platform="wati",
        project_public_id=project_public_id,
        request=request,
        signature=x_wati_signature,
        session=session,
    )


@router.post(
    "/respondio/{project_public_id}",
    response_model=WebhookAck,
    status_code=status.HTTP_200_OK,
)
@limiter.limit(WEBHOOK_RATE_LIMIT)
async def respondio_webhook(
    project_public_id: str,
    request: Request,
    x_respondio_signature: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db),
) -> WebhookAck:
    return await _handle_inbound(
        platform="respondio",
        project_public_id=project_public_id,
        request=request,
        signature=x_respondio_signature,
        session=session,
    )
