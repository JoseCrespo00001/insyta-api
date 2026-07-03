"""Segmentación de conversaciones (AUD-2.4).

Deriva en código (no LLM) una etiqueta de segmento por conversación a partir de
la rúbrica + el score + señales de fraude, para poder filtrar el reporte y armar
campañas. Se persiste en `Evaluation.segment`.

Segmentos: cliente_ideal | satisfecho | neutral | insatisfecho | potencial_lead
| problematico.
"""

from __future__ import annotations

from app.llm.schemas import RubricResponse
from app.services.rubric_scoring import ScoreResult

SEGMENTS = (
    "cliente_ideal",
    "satisfecho",
    "neutral",
    "insatisfecho",
    "potencial_lead",
    "problematico",
)


def _dim(rubric: RubricResponse, dim_id: str) -> int | None:
    for d in rubric.dimensiones:
        if d.id.upper() == dim_id and d.score is not None:
            return d.score
    return None


def derive_segment(rubric: RubricResponse, score: ScoreResult) -> str:
    """Etiqueta de segmento. Orden de prioridad pensado para no perder señales
    fuertes (fraude/insatisfacción) por debajo de un buen score."""
    end_sent = (
        rubric.sentimiento_trayectoria[-1] if rubric.sentimiento_trayectoria else None
    )
    e4 = _dim(rubric, "E4")  # sentimiento del usuario
    f1 = _dim(rubric, "F1")  # deseabilidad / apego
    f3 = _dim(rubric, "F3")  # oportunidad de venta
    sf = score.score_final

    # 1. Problemático: fraude o intento de jailbreak (aunque el resto sea bueno).
    if rubric.fraude_flags or "B1_jailbreak" in score.veto_flags:
        return "problematico"

    # 2. Insatisfecho: sentimiento negativo domina.
    if (e4 is not None and e4 <= 2) or end_sent == "negativo":
        return "insatisfecho"

    # 3. Cliente ideal: score alto + satisfecho + generó valor de negocio.
    if (
        sf is not None
        and sf >= 80
        and (e4 is None or e4 >= 4)
        and (f1 is not None and f1 >= 4)
    ):
        return "cliente_ideal"

    # 4. Potencial lead: mostró intención/apego de compra sin cierre claro.
    if (f3 is not None and f3 >= 3) or (f1 is not None and f1 >= 4):
        return "potencial_lead"

    # 5. Satisfecho: buena experiencia sin señal de venta.
    if (
        (e4 is not None and e4 >= 4)
        or end_sent == "positivo"
        or (sf is not None and sf >= 70)
    ):
        return "satisfecho"

    return "neutral"
