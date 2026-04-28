import hashlib
import hmac
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models import Project

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

SIGNATURE_PREFIX = "sha256="


class WebhookAck(BaseModel):
    received: bool
    platform: str


async def _load_project_secret(session: AsyncSession, project_public_id: str) -> str:
    result = await session.execute(
        select(Project.webhook_secret).where(Project.public_id == project_public_id)
    )
    secret = result.scalar_one_or_none()
    if secret is None:
        logger.warning(
            "[WEBHOOK] Unknown project public_id=%s — rejecting", project_public_id
        )
        raise HTTPException(status_code=401, detail="Invalid signature")
    return secret


def _verify_signature(secret: str, body: bytes, header_value: str) -> bool:
    if not header_value.startswith(SIGNATURE_PREFIX):
        return False
    provided = header_value[len(SIGNATURE_PREFIX) :]
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided, expected)


@router.post(
    "/wati/{project_public_id}",
    response_model=WebhookAck,
    status_code=status.HTTP_200_OK,
)
async def wati_webhook(
    project_public_id: str,
    request: Request,
    x_wati_signature: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db),
) -> WebhookAck:
    if not x_wati_signature:
        logger.info("[WEBHOOK_WATI] Missing signature header")
        raise HTTPException(status_code=401, detail="Missing signature header")

    body = await request.body()
    secret = await _load_project_secret(session, project_public_id)

    if not _verify_signature(secret, body, x_wati_signature):
        logger.warning(
            "[WEBHOOK_WATI] Invalid signature for project=%s", project_public_id
        )
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload: dict[str, Any] = await request.json()
    logger.info(
        "[WEBHOOK_WATI] Verified payload project=%s keys=%d",
        project_public_id,
        len(payload),
    )
    return WebhookAck(received=True, platform="wati")


@router.post(
    "/respondio/{project_public_id}",
    response_model=WebhookAck,
    status_code=status.HTTP_200_OK,
)
async def respondio_webhook(
    project_public_id: str,
    request: Request,
    x_respondio_signature: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db),
) -> WebhookAck:
    if not x_respondio_signature:
        logger.info("[WEBHOOK_RESPONDIO] Missing signature header")
        raise HTTPException(status_code=401, detail="Missing signature header")

    body = await request.body()
    secret = await _load_project_secret(session, project_public_id)

    if not _verify_signature(secret, body, x_respondio_signature):
        logger.warning(
            "[WEBHOOK_RESPONDIO] Invalid signature for project=%s",
            project_public_id,
        )
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload: dict[str, Any] = await request.json()
    logger.info(
        "[WEBHOOK_RESPONDIO] Verified payload project=%s keys=%d",
        project_public_id,
        len(payload),
    )
    return WebhookAck(received=True, platform="respondio")
