"""AUD-2.4: derivación de segmento por conversación."""

from __future__ import annotations

from app.llm.schemas import RubricResponse
from app.services.rubric_scoring import compute_scores
from app.services.segmentation import derive_segment


def _seg(dims, veto=None, fraude=None, traj=None):
    r = RubricResponse.model_validate(
        {
            "dimensiones": [
                {"id": i, "score": s, "turn_id": 1, "justificacion": "x"}
                for (i, s) in dims
            ],
            "veto_flags": veto or [],
            "fraude_flags": fraude or [],
            "sentimiento_trayectoria": traj or [],
            "confidence": 0.9,
        }
    )
    return derive_segment(r, compute_scores(r))


def test_problematico_by_fraud():
    assert _seg([("C1", 5)], fraude=["cbu_invalido_repetido"]) == "problematico"


def test_problematico_by_jailbreak():
    assert _seg([("C1", 5)], veto=["B1_jailbreak"]) == "problematico"


def test_insatisfecho_by_low_e4():
    assert _seg([("E4", 1), ("C1", 4)]) == "insatisfecho"


def test_insatisfecho_by_negative_trajectory():
    assert _seg([("C1", 4)], traj=["neutral", "negativo"]) == "insatisfecho"


def test_cliente_ideal():
    # score alto + satisfecho + apego de compra
    assert _seg([("A1", 5), ("C1", 5), ("E4", 5), ("F1", 5)]) == "cliente_ideal"


def test_potencial_lead():
    # oportunidad de venta detectada pero sin ser cliente ideal
    assert _seg([("C1", 3), ("F3", 4)]) == "potencial_lead"


def test_satisfecho():
    assert _seg([("E4", 4), ("C1", 4)]) == "satisfecho"


def test_neutral_default():
    assert _seg([("C1", 3)]) == "neutral"
