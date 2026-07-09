"""Per-message judge for audits (the 'reporte por mensaje').

Given a conversation's messages plus the audit's emphasis + free_text, returns a
verdict per assistant message: a label and, when problematic, the issue
taxonomy (type/subtype/severity) and a short note. Anthropic Haiku primary,
OpenAI gpt-4.1-mini fallback. Raises FatalLLMError if no provider key is set.
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from pydantic import BaseModel, ValidationError

from app.llm.router import FatalLLMError, TransientLLMError

logger = logging.getLogger(__name__)

# Taxonomía canónica de issue_type del judge por-mensaje. Fuente única de verdad:
# cualquier trigger que filtre por issue_type (p.ej. `unhandled` en workers/audit.py)
# debe usar valores de acá.
ISSUE_TYPES: tuple[str, ...] = (
    "alucinacion",
    "error_politica",
    "frustracion",
    "contradiccion",
    "alcance",
    "otro",
)

SYSTEM_PROMPT = """Sos un auditor de calidad de conversaciones de atención al cliente.
Recibís una conversación (mensajes anonimizados) y debés evaluar CADA mensaje del
asistente. Para cada mensaje del asistente devolvé un veredicto:
- label: "ok" | "warning" | "error"
- issue_type: cuando hay problema, una de: alucinacion | error_politica | frustracion | contradiccion | alcance | otro (si no hay, null)
- issue_subtype: subtipo corto en snake_case (o null)
- severity: "baja" | "media" | "alta" | "critica" (o null si label=ok)
- note: explicación breve (<=160 chars) de por qué (o null si ok)
IMPORTANTE sobre alucinación: si el contexto trae un bloque "FUENTE DE VERDAD"
(datos autoritativos del negocio: precios, stock, plazos, CBU, etc.), un dato del
bot que COINCIDE con esa fuente (o con el flujo esperado) NO es alucinación. Solo
marcá "alucinacion" si el bot afirma algo que CONTRADICE la fuente de verdad o que
no está respaldado por ella ni por el flujo. No penalices datos correctos.
Considerá especialmente los aspectos de énfasis y la instrucción libre del auditor.
Respondé SOLO JSON con esta forma: {"verdicts":[{"seq":N,"label":...,"issue_type":...,"issue_subtype":...,"severity":...,"note":...}]}
Incluí un verdict por cada mensaje con role=assistant, usando su seq."""


class MessageVerdict(BaseModel):
    seq: int
    label: Literal["ok", "warning", "error"]
    issue_type: str | None = None
    issue_subtype: str | None = None
    severity: Literal["baja", "media", "alta", "critica"] | None = None
    note: str | None = None


class _JudgeResponse(BaseModel):
    verdicts: list[MessageVerdict]


def _build_user_prompt(
    messages: list[dict],
    emphasis: list[str] | None,
    free_text: str | None,
    objective: str | None = None,
    flow_context: str | None = None,
) -> str:
    lines = []
    if objective:
        lines.append(f"Objetivo de la campaña: {objective}")
    if flow_context:
        lines.append(f"Contexto (empresa + flujo esperado):\n{flow_context}")
    if emphasis:
        lines.append(f"Énfasis: {', '.join(emphasis)}")
    if free_text:
        lines.append(f"Instrucción del auditor: {free_text}")
    lines.append(
        "Juzgá cada mensaje del asistente según si AYUDA a cumplir el objetivo y "
        "sigue el flujo/empresa; marcá error/warning cuando se desvía."
    )
    lines.append("Conversación:")
    for m in messages:
        content = m.get("content_anonymized") or m.get("content") or ""
        lines.append(f"[seq={m['seq']}] {m['role']}: {content}")
    return "\n".join(lines)


def _parse(text: str) -> list[MessageVerdict]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].lstrip()
    try:
        payload = json.loads(text)
        return _JudgeResponse(**payload).verdicts
    except (json.JSONDecodeError, ValidationError) as exc:
        raise TransientLLMError(f"judge returned bad shape: {exc}") from exc


async def _anthropic(user_prompt: str) -> str:
    from anthropic import APIError, AsyncAnthropic

    from app.llm.credentials import get_anthropic_key

    key = get_anthropic_key()
    if not key:
        raise FatalLLMError("ANTHROPIC_API_KEY not set")
    client = AsyncAnthropic(api_key=key)
    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            system=[{"type": "text", "text": SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": user_prompt}],
        )
    except APIError as exc:
        raise TransientLLMError(f"anthropic judge error: {exc}") from exc
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


async def _openai(user_prompt: str) -> str:
    from openai import APIError, AsyncOpenAI

    from app.llm.credentials import get_openai_key

    key = get_openai_key()
    if not key:
        raise FatalLLMError("OPENAI_API_KEY not set")
    client = AsyncOpenAI(api_key=key)
    try:
        resp = await client.chat.completions.create(
            model="gpt-4.1-mini",
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
    except APIError as exc:
        raise TransientLLMError(f"openai judge error: {exc}") from exc
    return resp.choices[0].message.content or ""


async def _deepseek(user_prompt: str) -> str:
    from openai import APIError, AsyncOpenAI

    from app.llm.credentials import (
        DEEPSEEK_BASE_URL,
        DEEPSEEK_MODEL,
        get_deepseek_key,
    )

    key = get_deepseek_key()
    if not key:
        raise FatalLLMError("DEEPSEEK_API_KEY not set")
    client = AsyncOpenAI(api_key=key, base_url=DEEPSEEK_BASE_URL)
    try:
        resp = await client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
    except APIError as exc:
        raise TransientLLMError(f"deepseek judge error: {exc}") from exc
    return resp.choices[0].message.content or ""


async def judge_messages(
    messages: list[dict],
    *,
    emphasis: list[str] | None = None,
    free_text: str | None = None,
    objective: str | None = None,
    flow_context: str | None = None,
    provider: str | None = None,
) -> list[MessageVerdict]:
    """Veredictos por mensaje. Motor elegido (`provider`) con fallback."""
    logger.info(
        "[AUDIT:MSG] judge provider=%s n_msgs=%d (sin conteo de tokens)",
        provider or "anthropic",
        len(messages),
    )
    user_prompt = _build_user_prompt(
        messages, emphasis, free_text, objective, flow_context
    )
    # Orden de intento según el motor elegido.
    chain = [_deepseek, _anthropic] if provider == "deepseek" else [_anthropic, _openai]
    text = None
    last_exc: Exception | None = None
    for fn in chain:
        try:
            text = await fn(user_prompt)
            break
        except (TransientLLMError, FatalLLMError) as exc:
            last_exc = exc
            continue
    if text is None:
        raise last_exc or FatalLLMError("no judge provider available")
    verdicts = _parse(text)
    logger.info("[AUDIT:MSG] judge devolvió verdicts=%d", len(verdicts))
    return verdicts
