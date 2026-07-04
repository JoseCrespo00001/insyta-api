"""AUD-8.1: auditoría solo-conversaciones (sin flujo).

Sin flow_summary ni precios, el pipeline de rúbrica igual produce un score y un
segmento a partir del eval holístico + la data de empresa. Documenta que el
proceso 3 (auditar conversaciones con la data de la empresa, sin flujo) funciona.
"""

from __future__ import annotations

from app.llm.schemas import EvaluationResponse
from app.services.rubric_mapping import map_eval_to_rubric
from app.services.rubric_scoring import compute_scores
from app.services.segmentation import derive_segment


def _eval(**kw) -> EvaluationResponse:
    base = {
        "score": 78,
        "resolution": True,
        "satisfaction": 4,
        "tone": "positive",
        "frustration": False,
        "escalated": False,
        "efficiency": 4,
        "scope_violation": False,
        "topic": "consulta",
        "summary": "Atención correcta sin flujo cargado.",
    }
    base.update(kw)
    return EvaluationResponse.model_validate(base)


def test_flowless_audit_produces_score_and_segment():
    # Sin verdicts, sin flow context, sin precios (audit solo-conversaciones).
    rubric = map_eval_to_rubric(_eval(), [], last_turn=3, det_veto=[], fraude_flags=[])
    score = compute_scores(rubric)
    assert score.score_final is not None and score.score_final > 0
    assert score.has_veto is False
    seg = derive_segment(rubric, score)
    assert seg in (
        "cliente_ideal",
        "satisfecho",
        "neutral",
        "potencial_lead",
    )


def test_flowless_still_flags_veto_from_det():
    # Aún sin flujo, un CBU inválido del bot (DET) topea el score.
    rubric = map_eval_to_rubric(
        _eval(), [], last_turn=2, det_veto=["A5_cbu_invalido"], fraude_flags=[]
    )
    score = compute_scores(rubric)
    assert score.has_veto is True
    assert score.score_final == 20
