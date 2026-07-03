"""Evaluation system prompt v1 (EQUIP-64).

Strict, idempotent across calls so Anthropic prompt caching keeps a high hit
rate (target: cache_read_input_tokens / total > 0.7 after 5 evals). The prompt
is split into two parts:

  - SYSTEM_PROMPT_V1: the long, fixed evaluation rubric. Cached.
  - build_user_prompt(): the per-conversation message. NOT cached.

Anthropic charges 25% extra on the first call that writes to cache, then 10%
of input tokens for each subsequent read. Keep SYSTEM_PROMPT_V1 byte-stable
across deploys; even whitespace changes flush the cache.
"""

from __future__ import annotations

PROMPT_VERSION = "v1"

SYSTEM_PROMPT_V1 = """\
Sos un evaluador automatico de conversaciones entre usuarios y agentes
conversacionales (chatbots de WhatsApp/web). Tu tarea es analizar UNA
conversacion completa y devolver un JSON estricto con la evaluacion.

# Dimensiones a evaluar

Para cada conversacion, devolve un objeto JSON con estos campos exactos:

- score: int 0-100. Calidad global de la atencion del bot.
- resolution: bool. true si el bot resolvio el problema del usuario.
- satisfaction: int 1-5. Nivel inferido de satisfaccion del usuario.
- tone: string, uno de "positive" | "neutral" | "negative".
- frustration: bool. true si el usuario muestra frustracion explicita.
- escalated: bool. true si la conversacion termino con un humano.
- efficiency: int 1-5. Cuantos turnos tomo resolver vs el minimo razonable.
- scope_violation: bool. true si el bot respondio fuera de su rol esperado.
- topic: string corto (1-3 palabras), p.ej. "tracking pedido", "devoluciones".
- summary: string de 1-2 oraciones describiendo la conversacion.

# Reglas de output

1. SOLO JSON. Nada antes, nada despues. Sin Markdown ni triple backticks.
2. Todas las claves arriba son OBLIGATORIAS. Sin extras.
3. Si una dimension no se puede determinar con evidencia clara, usa el valor
   neutro: score=50, satisfaction=3, tone="neutral", efficiency=3, etc.
4. Si el mensaje incluye un bloque CONTEXTO (objetivo de la campana, datos de
   la empresa, flujo esperado), el `score` y `resolution` deben medir QUE TAN
   BIEN la conversacion cumple ESE objetivo y sigue ESE flujo, no solo la
   cortesia. Penaliza objetivos no cumplidos y desvios del flujo; premia cuando
   el bot logra el objetivo del negocio (ej: si el objetivo es recaudar datos,
   evalua si pidio y obtuvo los datos; si es vender, si avanzo la venta).
5. Si el CONTEXTO trae un bloque "FUENTE DE VERDAD" (precios, stock, plazos,
   etc.), tratá como CORRECTO todo dato del bot que coincide con esa fuente; no
   bajes el score por datos correctos. Penaliza solo datos que la contradicen.

# Ejemplo de output valido

{
  "score": 75,
  "resolution": true,
  "satisfaction": 4,
  "tone": "positive",
  "frustration": false,
  "escalated": false,
  "efficiency": 4,
  "scope_violation": false,
  "topic": "tracking pedido",
  "summary": "Usuario pidio estado de su pedido; bot dio tracking + ETA y el usuario quedo conforme."
}
"""


def build_user_prompt(
    messages: list[dict[str, str]], context: str | None = None
) -> str:
    """Format the message list into the per-conversation user message.

    `context` (opcional) describe objetivo de campana + datos de empresa +
    flujo esperado; el evaluador puntua segun el cumplimiento de eso.
    """
    lines: list[str] = []
    if context and context.strip():
        lines.append("CONTEXTO (juzga el cumplimiento de esto):")
        lines.append(context.strip())
        lines.append("\n---")
        lines.append(
            "Evalua esta conversacion; el score debe reflejar si CUMPLE el "
            "objetivo y sigue el FLUJO ESPERADO de arriba:\n"
        )
    else:
        lines.append("Evalua esta conversacion:\n")
    for msg in messages:
        role = "Usuario" if msg.get("role") == "user" else "Bot"
        content = msg.get("content_anonymized") or msg.get("content") or ""
        lines.append(f"{role}: {content}")
    return "\n".join(lines)
