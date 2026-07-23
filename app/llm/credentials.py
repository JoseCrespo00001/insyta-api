"""Credenciales LLM por contexto de ejecución (SOLO keys por-org).

`run_audit` y el flow-audit setean vía `set_llm_keys` la API key que el tenant
cargó desde el front (cifrada en `organizations.*_api_key_encrypted`). Los
getters devuelven ÚNICAMENTE ese override: NO hay fallback a la key de
plataforma del `.env` — una org sin key propia no puede gastar la del entorno
(los providers fallan con FatalLLMError y el endpoint corta antes con 402).
"""

from __future__ import annotations

from contextvars import ContextVar

_anthropic: ContextVar[str | None] = ContextVar("anthropic_key", default=None)
_openai: ContextVar[str | None] = ContextVar("openai_key", default=None)
_deepseek: ContextVar[str | None] = ContextVar("deepseek_key", default=None)

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"


def set_llm_keys(
    *,
    anthropic: str | None = None,
    openai: str | None = None,
    deepseek: str | None = None,
) -> None:
    if anthropic:
        _anthropic.set(anthropic)
    if openai:
        _openai.set(openai)
    if deepseek:
        _deepseek.set(deepseek)


def get_anthropic_key() -> str | None:
    return _anthropic.get()


def get_openai_key() -> str | None:
    return _openai.get()


def get_deepseek_key() -> str | None:
    return _deepseek.get()
