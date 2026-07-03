"""Mapeo eval_v1 → rúbrica (AUD-7.0).

Para no gastar una segunda llamada al LLM por conversación, mapeamos la salida
del judge actual (eval_v1: score/resolution/satisfaction/tone/...) + los
veredictos por mensaje + los flags deterministas (validadores/fraude) a la
`RubricResponse`. Con eso `rubric_scoring` computa score_final/VETO y
`segmentation` deriva el segmento, poblando las columnas nuevas de Evaluation.

Cuando se agregue un judge de rúbrica dedicado, se reemplaza este mapeo.
"""

from __future__ import annotations

from app.llm.schemas import EvaluationResponse, RubricResponse

_TONE_TO_SCORE = {"positive": 5, "neutral": 3, "negative": 1}
_TONE_TO_SENT = {"positive": "positivo", "neutral": "neutral", "negative": "negativo"}


def _dim(dim_id: str, score: int | None, turn_id: int | None, why: str) -> dict:
    return {"id": dim_id, "score": score, "turn_id": turn_id, "justificacion": why}


def map_eval_to_rubric(
    parsed: EvaluationResponse,
    verdicts: list,
    *,
    last_turn: int,
    det_veto: list[str] | None = None,
    fraude_flags: list[str] | None = None,
) -> RubricResponse:
    """Construye una RubricResponse a partir del eval holístico + verdicts + DET.

    Las dimensiones derivadas del eval de conversación citan `last_turn` como
    evidencia representativa (el eval ya juzgó la charla completa).
    """
    # A1 (alucinación) desde los verdicts por mensaje.
    aluc = next(
        (v for v in verdicts if getattr(v, "issue_type", None) == "alucinacion"), None
    )
    a1_turn = getattr(aluc, "seq", last_turn) if aluc else last_turn
    a1_score = 1 if aluc else 5

    dims = [
        _dim(
            "A1",
            a1_score,
            a1_turn,
            "Alucinación detectada" if aluc else "Sin invención vs fuente",
        ),
        _dim("A4", 1 if parsed.scope_violation else 5, last_turn, "Alcance/política"),
        _dim("C2", 5 if parsed.resolution else 2, last_turn, "Resolución"),
        _dim("C4", parsed.efficiency, last_turn, "Comprensión/eficiencia"),
        _dim(
            "E1", _TONE_TO_SCORE.get(parsed.tone, 3), last_turn, f"Tono {parsed.tone}"
        ),
        _dim("E4", parsed.satisfaction, last_turn, "Sentimiento del usuario"),
    ]

    veto = list(det_veto or [])
    if aluc:
        veto.append("A1_alucinacion")

    sentiment = _TONE_TO_SENT.get(parsed.tone, "neutral")
    trajectory = ["neutral", sentiment] if sentiment != "neutral" else [sentiment]

    has_signal = bool(veto or fraude_flags)
    confidence = 0.5 if has_signal else 0.65  # mapeo holístico → confianza moderada

    return RubricResponse.model_validate(
        {
            "dimensiones": dims,
            "veto_flags": sorted(set(veto)),
            "confidence": confidence,
            "sentimiento_trayectoria": trajectory,
            "fraude_flags": sorted(set(fraude_flags or [])),
            "resumen": parsed.summary or "",
            "requiere_revision_humana": has_signal,
        }
    )


__all__ = ["map_eval_to_rubric"]
