"""AUD-2.3: fórmula de score + VETO determinista."""

from __future__ import annotations

from app.llm.schemas import RubricResponse
from app.services.rubric_scoring import VETO_CAP, compute_scores


def _rubric(dims, veto=None, confidence=0.9):
    return RubricResponse.model_validate(
        {
            "dimensiones": [
                {"id": i, "score": s, "turn_id": t, "justificacion": "x"}
                for (i, s, t) in dims
            ],
            "veto_flags": veto or [],
            "confidence": confidence,
        }
    )


def test_all_max_scores_100():
    r = _rubric([("A1", 5, 1), ("C1", 5, 2), ("E1", 5, 3)])
    res = compute_scores(r)
    assert res.score_bruto == 100
    assert res.score_final == 100
    assert res.has_veto is False


def test_all_min_scores_0():
    r = _rubric([("A1", 1, 1), ("C1", 1, 2)])
    res = compute_scores(r)
    assert res.score_bruto == 0
    assert res.score_final == 0


def test_veto_caps_final_at_20():
    # Score bruto alto pero con VETO → final topeado a 20, bruto se preserva.
    r = _rubric([("A1", 5, 1), ("C1", 5, 2)], veto=["A2_riesgo_legal"])
    res = compute_scores(r)
    assert res.score_bruto == 100
    assert res.score_final == VETO_CAP == 20
    assert res.has_veto is True
    assert "A2_riesgo_legal" in res.veto_flags


def test_extra_det_veto_merges():
    r = _rubric([("C1", 5, 1)])
    res = compute_scores(r, extra_veto=["A5_cbu_invalido"])
    assert res.has_veto is True
    assert res.score_final == 20
    assert res.veto_flags == ["A5_cbu_invalido"]


def test_score_without_evidence_ignored():
    # Dim sin turn_id → score null → no cuenta. Queda solo la que tiene evidencia.
    r = _rubric([("A1", 1, None), ("C1", 5, 2)])
    res = compute_scores(r)
    assert res.score_bruto == 100  # solo C1 (=5 → 100) cuenta


def test_empty_dims_no_score_but_veto_applies():
    r = _rubric([], veto=["A3_fuga_pii"])
    res = compute_scores(r)
    assert res.score_bruto is None
    assert res.score_final == 20
    assert res.has_veto is True


def test_confidence_preserved():
    r = _rubric([("C1", 3, 1)], confidence=0.42)
    assert compute_scores(r).confidence == 0.42
