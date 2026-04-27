import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class WebhookAck(BaseModel):
    received: bool
    platform: str


@router.post("/wati", response_model=WebhookAck, status_code=status.HTTP_200_OK)
async def wati_webhook(
    request: Request,
    x_wati_signature: str | None = Header(default=None),
) -> WebhookAck:
    payload: dict[str, Any] = await request.json()
    logger.info("[WEBHOOK_WATI] Received payload with %d keys", len(payload))

    if not x_wati_signature:
        raise HTTPException(status_code=401, detail="Missing signature header")

    return WebhookAck(received=True, platform="wati")


@router.post("/respondio", response_model=WebhookAck, status_code=status.HTTP_200_OK)
async def respondio_webhook(request: Request) -> WebhookAck:
    payload: dict[str, Any] = await request.json()
    logger.info("[WEBHOOK_RESPONDIO] Received payload with %d keys", len(payload))
    return WebhookAck(received=True, platform="respondio")
