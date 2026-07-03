"""AUD-2.2: RubricResponse parsea la salida del judge y aplica la regla de evidencia."""

from __future__ import annotations

from app.llm.prompts import rubric_v2
from app.llm.schemas import RubricResponse


def test_parses_wellformed_rubric():
    data = {
        "dimensiones": [
            {
                "id": "A1",
                "score": 5,
                "turn_id": 4,
                "justificacion": "precio coincide con precios.json",
            },
            {
                "id": "E4",
                "score": 2,
                "turn_id": 11,
                "justificacion": "usuario se enoja",
            },
        ],
        "veto_flags": ["A2_riesgo_legal"],
        "confidence": 0.8,
        "sentimiento_trayectoria": ["neutral", "negativo"],
        "resumen": "El bot cotizó bien pero prometió algo insostenible.",
        "requiere_revision_humana": False,
    }
    r = RubricResponse.model_validate(data)
    assert len(r.dimensiones) == 2
    assert r.dimensiones[0].score == 5
    assert r.veto_flags == ["A2_riesgo_legal"]
    assert r.confidence == 0.8


def test_score_requires_turn_id():
    """Sin turn_id el score se anula (no cuenta), aunque el LLM haya puesto un número."""
    r = RubricResponse.model_validate(
        {
            "dimensiones": [
                {"id": "C2", "score": 4, "turn_id": None, "justificacion": "x"}
            ]
        }
    )
    assert r.dimensiones[0].score is None


def test_prompt_lists_dimensions_and_anchors():
    assert "A1" in rubric_v2.SYSTEM_PROMPT
    assert "turn_id" in rubric_v2.SYSTEM_PROMPT
    assert "FUENTE DE VERDAD" in rubric_v2.SYSTEM_PROMPT
    up = rubric_v2.build_user_prompt(
        "OBJETIVO: ventas",
        [{"role": "user", "content": "hola", "content_anonymized": None}],
    )
    assert "turn_id=0" in up
