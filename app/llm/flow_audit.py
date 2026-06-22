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

from app.llm.audit_judge import FatalLLMError, TransientLLMError

logger = logging.getLogger(__name__)

# Conocimiento de Langflow embebido en el prompt (componentes, agentes, tools,
# ruteo, structured output, MCP, flows-as-tools, memoria, RAG, buenas prácticas).
_LANGFLOW_SYSTEM = """\
Sos un arquitecto experto en Langflow (la herramienta visual de orquestación de \
agentes sobre LangChain) y en diseño de agentes conversacionales de producción. \
Conocés a fondo las versiones recientes de Langflow y sus componentes:

- Entrada/Salida: Chat Input, Chat Output, Text Input/Output.
- Modelos: OpenAI, Anthropic, Groq, Google, modelos locales (Ollama).
- Agent: el componente Agent moderno con tool-calling nativo, instrucciones \
(system prompt), y conexión de Tools. Soporta múltiples herramientas.
- Tools: Calculator, URL/Web Search, Python REPL, API Request, herramientas \
custom, y "flows-as-tools" (un flujo puede exponerse como tool de un agente).
- MCP: componentes MCP server/client para conectar herramientas externas por \
protocolo MCP.
- Ruteo/condiciones: Conditional Router (If-Else), Pass, Loop, Listen/Notify.
- Prompts: Prompt template con variables; Structured Output para forzar JSON.
- Memoria: Chat Memory / Message History para mantener contexto.
- RAG: Vector Store (Astra, Chroma, pgvector), embeddings, retrievers, splitters.
- Sub-flows y composición: un supervisor que rutea a agentes especializados.

Principios de diseño que evaluás:
1. Un prompt gigante que mezcla muchas responsabilidades es un anti-patrón: \
conviene dividir en agentes especializados coordinados por un router/supervisor.
2. Cada agente debería tener tools concretas para CUMPLIR la tarea (no solo \
"responder"). Si un agente promete acciones (pagos, tracking, devoluciones) sin \
una tool/API que las ejecute, falta esa herramienta.
3. Tiene que haber ruteo claro cuando hay múltiples intenciones; si no, el \
mensaje cae en el agente equivocado.
4. Manejo de casos borde: pedir datos faltantes, fallback, escalar a humano, \
mensajes fuera de scope, errores de tool.
5. Memoria/contexto cuando la conversación es multi-turno.
6. Nodos deben estar conectados (entrada → ... → salida); nodos sueltos no se \
ejecutan.
7. Structured output cuando otro paso consume el resultado.

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
componentes que no existen en Langflow."""

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

    system = _LANGFLOW_SYSTEM + (_DEEP_EXTRA if mode == "deep" else "")
    user = "Resumen del flujo a auditar:\n\n" + summarize_flow(flow_json)
    client = AsyncAnthropic(api_key=key)
    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096 if mode == "deep" else 2048,
            system=[{"type": "text", "text": system}],
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
