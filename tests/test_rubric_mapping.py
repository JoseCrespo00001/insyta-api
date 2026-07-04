"""AUD-7.0: mapeo eval_v1 → rúbrica + integración con scoring/segmentación."""

from __future__ import annotations

from types import SimpleNamespace

from app.llm.schemas import EvaluationResponse
from app.services.rubric_mapping import map_eval_to_rubric
from app.services.rubric_scoring import compute_scores
from app.services.segmentation import derive_segment


def _eval(**kw) -> EvaluationResponse:
    base = {
        "score": 85,
        "resolution": True,
        "satisfaction": 5,
        "tone": "positive",
        "frustration": False,
        "escalated": False,
        "efficiency": 5,
        "scope_violation": False,
        "topic": "limpieza",
        "summary": "El bot cotizó bien y el usuario quedó conforme.",
    }
    base.update(kw)
    return EvaluationResponse.model_validate(base)


def _verdict(seq, issue_type=None):
    return SimpleNamespace(seq=seq, issue_type=issue_type)


def test_clean_eval_maps_high_score_no_veto():
    r = map_eval_to_rubric(_eval(), [], last_turn=5)
    assert r.veto_flags == []
    # todas las dims tienen turn_id → score cuenta
    assert all(d.score is not None for d in r.dimensiones)
    sc = compute_scores(r)
    assert sc.score_final is not None and sc.score_final >= 80
    assert sc.has_veto is False


def test_alucinacion_verdict_triggers_veto_and_caps():
    verdicts = [_verdict(3, "alucinacion")]
    r = map_eval_to_rubric(_eval(), verdicts, last_turn=6)
    assert "A1_alucinacion" in r.veto_flags
    a1 = next(d for d in r.dimensiones if d.id == "A1")
    assert a1.score == 1 and a1.turn_id == 3
    sc = compute_scores(r)
    assert sc.has_veto is True
    assert sc.score_final == 20  # topeado por VETO


def test_det_veto_and_fraud_merge():
    r = map_eval_to_rubric(
        _eval(),
        [],
        last_turn=2,
        det_veto=["A5_cbu_invalido"],
        fraude_flags=["pago_fuera_canal"],
    )
    assert "A5_cbu_invalido" in r.veto_flags
    assert "pago_fuera_canal" in r.fraude_flags


def test_segment_from_mapped_rubric():
    r = map_eval_to_rubric(_eval(satisfaction=5, tone="positive"), [], last_turn=4)
    seg = derive_segment(r, compute_scores(r))
    assert seg in ("cliente_ideal", "satisfecho", "potencial_lead")


def test_insatisfecho_maps_low_e4():
    r = map_eval_to_rubric(
        _eval(satisfaction=1, tone="negative", resolution=False), [], last_turn=4
    )
    assert derive_segment(r, compute_scores(r)) == "insatisfecho"
