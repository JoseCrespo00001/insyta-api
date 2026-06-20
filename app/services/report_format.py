"""Serialización compartida de Evaluation al contrato camelCase del front.

El tipo `ConversationEvaluation` (insyta-web `lib/projects/types.ts`) lo consumen
`ReportView` (auditorías) y `ConversationReport` (workspace). Un único formateador
evita que los dos endpoints (audits + conversations) diverjan.
"""

from __future__ import annotations

from app.models import Evaluation

_EMPTY_EVAL: dict = {
    "resolution": False,
    "satisfaction": 0,
    "tone": "neutral",
    "frustration": False,
    "escalated": False,
    "efficiency": 0,
    "scopeViolation": False,
    "topic": "",
    "summary": "",
    "modelUsed": "",
    "tokensInput": 0,
    "tokensOutput": 0,
    "costUsd": 0,
    "latencyMs": 0,
    "phoenixTraceId": "",
    "phoenixSpanId": "",
    "evaluatedAt": "",
}


def eval_to_camel(ev: Evaluation | None) -> dict:
    """ConversationEvaluation (camelCase). Ceros si la conversación aún no fue
    evaluada por el judge (auditoría en curso o no corrida)."""
    if ev is None:
        return dict(_EMPTY_EVAL)
    return {
        "resolution": bool(ev.resolution),
        "satisfaction": ev.satisfaction or 0,
        "tone": ev.tone or "neutral",
        "frustration": bool(ev.frustration),
        "escalated": bool(ev.escalated),
        "efficiency": ev.efficiency or 0,
        "scopeViolation": bool(ev.scope_violation),
        "topic": ev.topic or "",
        "summary": ev.summary or "",
        "modelUsed": ev.model_used or "",
        "tokensInput": ev.tokens_input or 0,
        "tokensOutput": ev.tokens_output or 0,
        "costUsd": float(ev.cost_usd) if ev.cost_usd is not None else 0,
        "latencyMs": ev.latency_ms or 0,
        "phoenixTraceId": ev.phoenix_trace_id or "",
        "phoenixSpanId": ev.phoenix_span_id or "",
        "evaluatedAt": ev.evaluated_at.isoformat() if ev.evaluated_at else "",
    }
