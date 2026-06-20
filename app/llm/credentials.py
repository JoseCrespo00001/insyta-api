"""Override de credenciales LLM por contexto de ejecución.

Permite que `run_audit` use la API key que el tenant cargó desde el front (por-org)
en vez de la global de entorno. Si no hay override, cae a `settings`.
"""

from __future__ import annotations

from contextvars import ContextVar

from app.core.config import get_settings

_anthropic: ContextVar[str | None] = ContextVar("anthropic_key", default=None)
_openai: ContextVar[str | None] = ContextVar("openai_key", default=None)


def set_llm_keys(*, anthropic: str | None = None, openai: str | None = None) -> None:
    if anthropic:
        _anthropic.set(anthropic)
    if openai:
        _openai.set(openai)


def get_anthropic_key() -> str | None:
    return _anthropic.get() or get_settings().anthropic_api_key


def get_openai_key() -> str | None:
    return _openai.get() or get_settings().openai_api_key
