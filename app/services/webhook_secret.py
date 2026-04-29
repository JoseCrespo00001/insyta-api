"""Encrypt webhook secrets at rest using Fernet (AES-128-CBC + HMAC-SHA256).

We can't bcrypt the secret because the HMAC validator needs the original value
to recompute the signature on every inbound webhook. Plain text in DB would
mean a single dump leaks every customer's webhook. Fernet keeps the secret
recoverable for HMAC while still requiring the WEBHOOK_SECRET_KEY environment
variable to read it back.

Key rotation: change WEBHOOK_SECRET_KEY and re-encrypt existing rows offline.
"""

from __future__ import annotations

import logging
import secrets
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings

logger = logging.getLogger(__name__)

WEBHOOK_SECRET_LENGTH_BYTES = 32


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = get_settings().webhook_secret_key.get_secret_value().encode()
    if not key:
        raise RuntimeError("webhook_secret_key not configured")
    return Fernet(key)


def generate_raw_webhook_secret() -> str:
    """URL-safe random secret returned to the user once, never stored in plain."""
    return secrets.token_urlsafe(WEBHOOK_SECRET_LENGTH_BYTES)


def encrypt_webhook_secret(raw: str) -> bytes:
    return _fernet().encrypt(raw.encode())


def decrypt_webhook_secret(encrypted: bytes) -> str:
    try:
        return _fernet().decrypt(encrypted).decode()
    except InvalidToken as exc:
        logger.error(
            "[WEBHOOK_SECRET] Decryption failed — wrong key or tampered ciphertext"
        )
        raise RuntimeError("webhook secret decryption failed") from exc
