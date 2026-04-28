"""Stub endpoints for the SDK pre-release.

`/api/v1/track` and `/api/v1/message` are advertised by `insyta-sdk` but the
backend implementations land in Sprint 2. Returning a deterministic 503 with a
JSON body lets the SDK surface a meaningful error instead of a generic 404.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["sdk"])

_BODY = {
    "error": "Insyta API beta — endpoint not yet available",
    "eta": "Sprint 2",
}


@router.post("/track", status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
async def track() -> JSONResponse:
    logger.info("[SDK_STUB] /track called pre-Sprint-2")
    return JSONResponse(status_code=503, content=_BODY)


@router.post("/message", status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
async def message() -> JSONResponse:
    logger.info("[SDK_STUB] /message called pre-Sprint-2")
    return JSONResponse(status_code=503, content=_BODY)
