"""Scoring determinista de la rúbrica (AUD-2.3).

Toma la salida del judge (`RubricResponse`) + flags VETO deterministas (CBU, PII,
etc. — Fase 3) y computa el score. NO lo hace el LLM: acá está la fórmula
reproducible (rúbrica §4).

- score_bruto = Σ(peso_categoría x promedio_normalizado_categoría) sobre las
  categorías presentes (renormalizado), 0-100.
- Si hay algún flag VETO → score_final = min(score_bruto, 20). Guardamos ambos.
- confidence viene del judge (cobertura de evidencia); se preserva.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.llm.schemas import RubricResponse

# Pesos por categoría (rúbrica §2). Suman 1.0 pero se renormalizan sobre las
# categorías realmente presentes (p.ej. D/SLA se calcula aparte en Fase 3).
CATEGORY_WEIGHTS: dict[str, float] = {
    "A": 0.30,  # veracidad / riesgo
    "B": 0.10,  # seguridad
    "C": 0.20,  # calidad de respuesta
    "D": 0.15,  # operativo / SLA
    "E": 0.15,  # tono y experiencia
    "F": 0.10,  # conversión / negocio
}
VETO_CAP = 20


@dataclass(frozen=True)
class ScoreResult:
    score_bruto: int | None
    score_final: int | None
    has_veto: bool
    veto_flags: list[str]
    confidence: float


def _norm(score_1_5: int) -> float:
    """1-5 → 0-100 (1→0, 3→50, 5→100)."""
    return (score_1_5 - 1) / 4 * 100.0


def compute_scores(
    rubric: RubricResponse, extra_veto: list[str] | None = None
) -> ScoreResult:
    # Agrupa dims con evidencia (score no nulo) por categoría (primera letra del id).
    by_cat: dict[str, list[int]] = {}
    for d in rubric.dimensiones:
        if d.score is None:
            continue
        cat = (d.id[:1] or "").upper()
        if cat in CATEGORY_WEIGHTS:
            by_cat.setdefault(cat, []).append(d.score)

    veto_set: set[str] = {*rubric.veto_flags, *(extra_veto or [])}
    veto_flags: list[str] = sorted(veto_set)
    has_veto = len(veto_flags) > 0

    if not by_cat:
        # Sin dimensiones puntuables: no hay score de calidad, pero el veto igual aplica.
        score_bruto = None
        score_final = VETO_CAP if has_veto else None
        return ScoreResult(
            score_bruto, score_final, has_veto, veto_flags, float(rubric.confidence)
        )

    total_w = sum(CATEGORY_WEIGHTS[c] for c in by_cat)
    weighted = sum(
        CATEGORY_WEIGHTS[c] * (sum(_norm(s) for s in scores) / len(scores))
        for c, scores in by_cat.items()
    )
    score_bruto = round(weighted / total_w)
    score_final = min(score_bruto, VETO_CAP) if has_veto else score_bruto
    return ScoreResult(
        score_bruto, score_final, has_veto, veto_flags, float(rubric.confidence)
    )
