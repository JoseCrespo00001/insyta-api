"""Auditoría del flujo en sí (no de conversaciones).

Recorre el export de Langflow, lo resume de forma compacta y se lo pasa a un LLM
con conocimiento experto de Langflow para que devuelva sugerencias de mejora:
qué tool agregar, si conviene multi-agente, si el prompt es muy largo, qué
condiciones/ramas faltan, nodos sueltos, casos sin cubrir, etc.

`mode="standard"` da un análisis enfocado; `mode="deep"` (Plus) recorre todos
los casos posibles y propone el flujo "completo".
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.llm.audit_judge import FatalLLMError, TransientLLMError

logger = logging.getLogger(__name__)

# Skill file editable con el conocimiento de Langflow. Se recarga en caliente.
_KNOWLEDGE_PATH = Path(__file__).parent / "knowledge" / "langflow.md"
_knowledge_cache: dict = {"mtime": None, "text": None}

# Fallback mínimo si el skill file no existe.
_KNOWLEDGE_FALLBACK = (
    "Langflow es una herramienta visual para construir agentes sobre LangChain. "
    "Evaluá: prompts monolíticos (dividir en agentes), agentes que prometen "
    "acciones sin tools, falta de ruteo con múltiples intenciones, casos borde "
    "(datos faltantes, fuera de scope, escalamiento), memoria multi-turno, nodos "
    "sueltos, entrada/salida, structured output y grounding/RAG."
)

_ROLE = (
    "Sos un arquitecto experto en Langflow y en diseño de agentes "
    "conversacionales de producción. Usá el siguiente conocimiento de Langflow "
    "para analizar el flujo:\n\n"
)

# El formato de salida vive en código (estable); el conocimiento, en el skill file.
_OUTPUT_SCHEMA = """

Te paso un RESUMEN del flujo (nodos con su tipo, nombre, prompt/instrucciones, \
modelo y tools; más las conexiones). Analizalo y devolvé SOLO un JSON válido con \
esta forma exacta (sin texto extra, sin markdown):

{
  "completeness": <int 0-100, qué tan completo/listo para producción está>,
  "summary": "<2-3 frases en español sobre el estado del flujo>",
  "suggestions": [
    {
      "type": "add_tool|multi_agent|split_prompt|add_condition|add_memory|add_fallback|structure|rag|other",
      "title": "<título corto accionable, español>",
      "detail": "<qué hacer y por qué, concreto, español>",
      "target": "<nombre del nodo/agente afectado o 'flujo'>",
      "severity": "info|warning|critical",
      "impact": "<beneficio esperado, ej: '+resolución', 'evita respuestas falsas'>"
    }
  ]
}

Sé específico y accionable; nombrá nodos reales del flujo. No inventes \
componentes que no existan en Langflow."""


def load_knowledge() -> str:
    """Lee el skill file de Langflow con cache por mtime (hot reload)."""
    try:
        st = _KNOWLEDGE_PATH.stat()
    except OSError:
        return _KNOWLEDGE_FALLBACK
    if _knowledge_cache["mtime"] != st.st_mtime:
        try:
            _knowledge_cache["text"] = _KNOWLEDGE_PATH.read_text(encoding="utf-8")
            _knowledge_cache["mtime"] = st.st_mtime
        except OSError:
            return _KNOWLEDGE_FALLBACK
    return _knowledge_cache["text"] or _KNOWLEDGE_FALLBACK


_DEEP_EXTRA = """\

MODO COMPLETO (Plus): sé exhaustivo. Recorré TODOS los casos posibles que un \
usuario real podría disparar y verificá si el flujo los cubre (consultas, \
acciones, errores, ambigüedad, datos faltantes, multi-intención, idioma, \
escalamiento). Proponé el flujo COMPLETO: qué agentes, tools, condiciones y \
memoria debería tener para estar listo para producción. Devolvé más sugerencias \
y más detalladas."""


def _node_summary(node: dict) -> dict:
    data = node.get("data", {}) or {}
    inner = data.get("node", {}) or {}
    template = inner.get("template", {}) or {}
    ntype = data.get("type") or node.get("type") or ""
    display = inner.get("display_name") or ntype

    def _tv(key: str) -> str:
        field = template.get(key)
        if isinstance(field, dict):
            val = field.get("value")
            if isinstance(val, str) and val.strip():
                return val.strip()[:600]
        return ""

    # Campos relevantes según el tipo de nodo.
    info: dict = {"id": node.get("id"), "type": ntype, "name": display}
    for key in (
        "system_prompt",
        "agent_instructions",
        "instructions",
        "template",
        "input_value",
        "match_text",
        "operator",
        "model_name",
        "model",
    ):
        v = _tv(key)
        if v:
            info[key] = v
    # Tools conectadas (si el template las declara).
    tools = template.get("tools")
    if isinstance(tools, dict) and tools.get("value"):
        info["tools"] = str(tools.get("value"))[:300]
    return {k: v for k, v in info.items() if v}


def summarize_flow(flow_json: dict) -> str:
    data = flow_json.get("data", flow_json)
    nodes = data.get("nodes", []) if isinstance(data, dict) else []
    edges = data.get("edges", []) if isinstance(data, dict) else []

    node_lines = [_node_summary(n) for n in nodes]
    id_to_name = {n.get("id"): (n.get("name") or n.get("id")) for n in node_lines}
    edge_lines = []
    for e in edges:
        src = id_to_name.get(e.get("source"), e.get("source"))
        tgt = id_to_name.get(e.get("target"), e.get("target"))
        if src and tgt:
            edge_lines.append(f"{src} -> {tgt}")

    return json.dumps(
        {"nodes": node_lines, "edges": edge_lines},
        ensure_ascii=False,
        indent=2,
    )


def _parse(text: str) -> dict:
    """Extrae el JSON de la respuesta del LLM de forma robusta."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise TransientLLMError("flow audit: respuesta sin JSON")
    return json.loads(text[start : end + 1])


async def audit_flow(flow_json: dict, *, mode: str = "standard") -> dict:
    """Devuelve {completeness, summary, suggestions[]}. Usa Anthropic."""
    from anthropic import APIError, AsyncAnthropic

    from app.llm.credentials import get_anthropic_key

    key = get_anthropic_key()
    if not key:
        raise FatalLLMError("ANTHROPIC_API_KEY not set")

    # El conocimiento (skill file) va en un bloque con cache_control para abaratar
    # auditorías repetidas; las instrucciones de formato/modo, en otro bloque.
    knowledge_block = _ROLE + load_knowledge()
    instructions = _OUTPUT_SCHEMA + (_DEEP_EXTRA if mode == "deep" else "")
    user = "Resumen del flujo a auditar:\n\n" + summarize_flow(flow_json)
    client = AsyncAnthropic(api_key=key)
    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096 if mode == "deep" else 2048,
            system=[
                {
                    "type": "text",
                    "text": knowledge_block,
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": instructions},
            ],
            messages=[{"role": "user", "content": user}],
        )
    except APIError as exc:
        raise TransientLLMError(f"flow audit error: {exc}") from exc

    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    parsed = _parse(text)
    # Normalización defensiva.
    suggestions = parsed.get("suggestions") or []
    return {
        "completeness": int(parsed.get("completeness") or 0),
        "summary": str(parsed.get("summary") or ""),
        "suggestions": [
            {
                "type": str(s.get("type") or "other"),
                "title": str(s.get("title") or ""),
                "detail": str(s.get("detail") or ""),
                "target": str(s.get("target") or "flujo"),
                "severity": str(s.get("severity") or "info"),
                "impact": str(s.get("impact") or ""),
            }
            for s in suggestions
            if isinstance(s, dict) and s.get("title")
        ],
        "mode": mode,
    }
