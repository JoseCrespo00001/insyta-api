"""SPC — cartas de control por agente (rúbrica §8, AUD-6.1).

El diferencial de Insyta: el score de una conversación vale como PUNTO en una
carta de control por agente, no como número aislado. Detecta deriva (un agente
que empeora) ANTES de que se caiga el promedio, con reglas Western Electric.

Funciones puras (sin DB); AUD-6.2 las conecta a agent_reputation.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

# Reglas Western Electric implementadas.
RULE_BEYOND_3SIGMA = "1_fuera_3sigma"
RULE_8_SAME_SIDE = "8_de_un_lado"
RULE_TREND_6 = "tendencia_6"

_TREND_LEN = 6
_SAME_SIDE_LEN = 8


@dataclass(frozen=True)
class Baseline:
    mean: float
    std: float
    ucl: float  # mean + 3 sigma
    lcl: float  # mean - 3 sigma
    n: int


@dataclass(frozen=True)
class SpcViolation:
    rule: str
    index: int  # índice del punto que dispara
    direction: str  # "alto" | "bajo"


def compute_baseline(values: list[float], n: int = 20) -> Baseline | None:
    """Baseline con las primeras `n` muestras 'en control'. Necesita >= 2 puntos."""
    sample = [float(v) for v in values[:n] if v is not None]
    if len(sample) < 2:
        return None
    mean = statistics.fmean(sample)
    std = statistics.stdev(sample)
    return Baseline(mean, std, mean + 3 * std, mean - 3 * std, len(sample))


def check_rules(values: list[float], baseline: Baseline) -> list[SpcViolation]:
    """Aplica las reglas Western Electric sobre la serie completa."""
    pts = [float(v) for v in values if v is not None]
    out: list[SpcViolation] = []

    # Regla 1: 1 punto fuera de 3 sigma.
    for i, v in enumerate(pts):
        if v > baseline.ucl:
            out.append(SpcViolation(RULE_BEYOND_3SIGMA, i, "alto"))
        elif v < baseline.lcl:
            out.append(SpcViolation(RULE_BEYOND_3SIGMA, i, "bajo"))

    # Regla 2: 8 puntos seguidos del mismo lado de la media.
    run_side = 0
    side = 0  # +1 arriba, -1 abajo
    for i, v in enumerate(pts):
        cur = 1 if v > baseline.mean else (-1 if v < baseline.mean else 0)
        if cur != 0 and cur == side:
            run_side += 1
        else:
            side, run_side = cur, 1 if cur != 0 else 0
        if run_side >= _SAME_SIDE_LEN:
            out.append(
                SpcViolation(RULE_8_SAME_SIDE, i, "alto" if side > 0 else "bajo")
            )

    # Regla 3: tendencia de 6 puntos consecutivos subiendo o bajando.
    up = down = 1
    for i in range(1, len(pts)):
        if pts[i] > pts[i - 1]:
            up, down = up + 1, 1
        elif pts[i] < pts[i - 1]:
            down, up = down + 1, 1
        else:
            up = down = 1
        if up >= _TREND_LEN:
            out.append(SpcViolation(RULE_TREND_6, i, "alto"))
        if down >= _TREND_LEN:
            out.append(SpcViolation(RULE_TREND_6, i, "bajo"))

    return out


def detect_drift(values: list[float], baseline: Baseline) -> str:
    """Etiqueta de tendencia del agente: degradacion | mejora | estable.

    Prioriza la deriva 'bajo' (empeora) sobre 'alto' (mejora)."""
    violations = check_rules(values, baseline)
    if not violations:
        return "estable"
    dirs = {v.direction for v in violations}
    if "bajo" in dirs:
        return "degradacion"
    if "alto" in dirs:
        return "mejora"
    return "estable"


@dataclass(frozen=True)
class SpcSummary:
    baseline: Baseline
    trend: str  # degradacion | mejora | estable
    violations: list[SpcViolation]


def spc_summary(values: list[float], baseline_n: int = 20) -> SpcSummary | None:
    """Baseline + deriva de una serie de scores por agente. None si no hay datos."""
    baseline = compute_baseline(values, n=baseline_n)
    if baseline is None:
        return None
    violations = check_rules(values, baseline)
    return SpcSummary(baseline, detect_drift(values, baseline), violations)


__all__ = [
    "RULE_8_SAME_SIDE",
    "RULE_BEYOND_3SIGMA",
    "RULE_TREND_6",
    "Baseline",
    "SpcSummary",
    "SpcViolation",
    "check_rules",
    "compute_baseline",
    "detect_drift",
    "spc_summary",
]
