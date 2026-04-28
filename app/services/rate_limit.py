"""Rate limiting with slowapi backed by Redis.

Use `limiter` as a decorator on routes (`@limiter.limit("1000/minute")`) and
register the `_rate_limit_exceeded_handler` in `main.create_app`.
"""

from __future__ import annotations

import logging

from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def _build_limiter() -> Limiter:
    settings = get_settings()
    return Limiter(
        key_func=get_remote_address,
        storage_uri=settings.redis_url,
        strategy="fixed-window",
        default_limits=[],
    )


limiter: Limiter = _build_limiter()

WEBHOOK_RATE_LIMIT = "1000/minute"


__all__ = ["RateLimitExceeded", "WEBHOOK_RATE_LIMIT", "limiter"]
