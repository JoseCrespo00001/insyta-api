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
# Umbral de confianza para que un VETO del LLM sea FIRME (B7). Por encima →
# firme (topea el score a 20). Por debajo → tentativo ("a confirmar", no topea con
# la misma dureza). Los validadores deterministas (CBU/precio) son SIEMPRE firmes.
# Configurable vía Settings.veto_confidence_threshold; este es el default.
VETO_CONFIDENCE_THRESHOLD = 0.6
# Flags deterministas por naturaleza: son un HECHO verificable, no un juicio del
# LLM, así que topean el score aunque el caller no re-pase `det_veto` y sin importar
# la confianza (B7). `A5_cbu_invalido` sale solo de `validate_cbu` (checksum); el
# judge nunca lo emite. `A1_alucinacion` NO está acá: es ambiguo (precio determinista
# vs alucinación del LLM) y su firmeza depende de `det_veto`.
ALWAYS_FIRM_VETO_FLAGS: frozenset[str] = frozenset({"A5_cbu_invalido"})


@dataclass(frozen=True)
class ScoreResult:
    score_bruto: int | None
    score_final: int | None
    has_veto: bool
    veto_flags: list[str]
    confidence: float
    # B7: firme (DET o confianza ≥ umbral → topea) vs tentativo (LLM con confianza
    # baja → no topea, se marca "a confirmar"). Solo relevante si has_veto.
    veto_firm: bool = False


def _norm(score_1_5: int) -> float:
    """1-5 → 0-100 (1→0, 3→50, 5→100)."""
    return (score_1_5 - 1) / 4 * 100.0


def compute_scores(
    rubric: RubricResponse,
    extra_veto: list[str] | None = None,
    *,
    det_veto: list[str] | None = None,
    confidence_threshold: float = VETO_CONFIDENCE_THRESHOLD,
) -> ScoreResult:
    # Agrupa dims con evidencia (score no nulo) por categoría (primera letra del id).
    by_cat: dict[str, list[int]] = {}
    for d in rubric.dimensiones:
        if d.score is None:
            continue
        cat = (d.id[:1] or "").upper()
        if cat in CATEGORY_WEIGHTS:
            by_cat.setdefault(cat, []).append(d.score)

    # `det_veto` alias legado `extra_veto`: flags DETERMINISTAS (siempre firmes).
    det_set: set[str] = {*(det_veto or []), *(extra_veto or [])}
    veto_set: set[str] = {*rubric.veto_flags, *det_set}
    veto_flags: list[str] = sorted(veto_set)
    has_veto = len(veto_flags) > 0
    confidence = float(rubric.confidence)
    # Firme si hay un flag determinista (pasado por el caller o determinista por
    # naturaleza como el CBU), o si el VETO del LLM tiene confianza alta. El resto
    # (LLM con confianza baja) queda tentativo y NO topea.
    firm_set = det_set | (veto_set & ALWAYS_FIRM_VETO_FLAGS)
    llm_flags = veto_set - firm_set
    veto_firm = has_veto and (
        bool(firm_set) or (bool(llm_flags) and confidence >= confidence_threshold)
    )

    if not by_cat:
        # Sin dimensiones puntuables: no hay score de calidad. El veto FIRME igual
        # topea; el tentativo no puede topear (no hay score que topear).
        score_bruto = None
        score_final = VETO_CAP if veto_firm else None
        return ScoreResult(score_bruto, score_final, has_veto, veto_flags, confidence, veto_firm)

    total_w = sum(CATEGORY_WEIGHTS[c] for c in by_cat)
    weighted = sum(
        CATEGORY_WEIGHTS[c] * (sum(_norm(s) for s in scores) / len(scores))
        for c, scores in by_cat.items()
    )
    score_bruto = round(weighted / total_w)
    # Solo el VETO FIRME topea a 20; el tentativo deja el bruto (se marca aparte).
    score_final = min(score_bruto, VETO_CAP) if veto_firm else score_bruto
    return ScoreResult(score_bruto, score_final, has_veto, veto_flags, confidence, veto_firm)
