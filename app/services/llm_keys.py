"""Resolución de la API key LLM por-org (cifrada en `organizations`).

Fuente única del mapeo provider → columna cifrada y del descifrado. La usan:
  - los routers que disparan trabajo LLM (guard 402 ANTES de encolar/llamar);
  - el worker `run_audit` (defensa en profundidad: sin key propia el audit
    falla — NUNCA se cae a la key de plataforma del `.env`).
"""

from __future__ import annotations

from app.models import Organization
from app.services.secret_crypto import decrypt_secret

# provider -> columna cifrada en organizations.
PROVIDER_KEY_COLUMNS = {
    "anthropic": "anthropic_api_key_encrypted",
    "deepseek": "deepseek_api_key_encrypted",
}

_PROVIDER_LABELS = {
    "anthropic": "Anthropic",
    "deepseek": "DeepSeek",
}


def get_org_llm_key(org: Organization | None, provider: str) -> str | None:
    """Key por-org descifrada para `provider`, o None si falta o está corrupta."""
    col = PROVIDER_KEY_COLUMNS.get(provider)
    if org is None or col is None:
        return None
    enc: str | None = getattr(org, col, None)
    if not enc:
        return None
    return decrypt_secret(enc)


def missing_key_detail(provider: str) -> str:
    """Mensaje accionable para el 402 / el error del audit failed."""
    label = _PROVIDER_LABELS.get(provider, provider)
    return (
        f"Tu organización no tiene API key de {label} configurada. "
        "Cargala en Configuración → API keys."
    )
