"""Cifrado simétrico para secretos guardados en DB (ej. API key del proveedor LLM).

Fernet con clave derivada del `jwt_secret` (siempre presente y >=32 chars), así no
hace falta configurar otra variable. La key NUNCA se guarda en claro.
"""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    secret = get_settings().jwt_secret.encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def mask_secret(plaintext: str) -> str:
    """sk-ant-...AB12 — para mostrar en la UI sin revelar la key."""
    if len(plaintext) <= 8:
        return "••••"
    return f"{plaintext[:6]}…{plaintext[-4:]}"
