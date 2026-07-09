"""Métricas del reporte con una sola fuente de verdad (fix B1/B2/B4).

Antes había cálculos duplicados/inconsistentes entre el worker y los routers:
- el score visible mostraba el crudo aunque hubiera VETO (B1);
- la distribución de satisfacción se contaba en dos lados con resultados distintos (B2);
- los duplicados de determinismo inflaban los agregados (B4).

Este módulo centraliza esas tres reglas para que back (worker + routers) computen
lo mismo y el front solo consuma.
"""

from __future__ import annotations

import hashlib
import re

# 5,4 → satisfecho · 3 → neutral · resto/None → insatisfecho.
SATISFACTION_BUCKETS = {5: "satisfecho", 4: "satisfecho", 3: "neutral"}

_PHONE_RE = re.compile(r"^\+?[\d][\d\s\-()]{5,}$")


def is_phone_like(value: str | None) -> bool:
    """True si el string parece un teléfono (PII a no mostrar crudo, B5)."""
    return bool(value and _PHONE_RE.match(value.strip()))


def visible_score(score: int | None, score_final: int | None) -> int | None:
    """El score que ve el usuario (B1): el topeado por VETO cuando existe, si no
    el crudo. `score_final` ya viene topeado a 20 por `rubric_scoring` cuando hay
    VETO firme; para conversaciones sin rúbrica cae al `score` del eval."""
    return score_final if score_final is not None else score


def satisfaction_bucket(satisfaction: int | None) -> str:
    return SATISFACTION_BUCKETS.get(satisfaction or 0, "insatisfecho")


def satisfaction_distribution(satisfactions: list[int | None]) -> dict[str, int]:
    """Distribución {satisfecho, neutral, insatisfecho} — un solo cálculo (B2)."""
    buckets = {"satisfecho": 0, "neutral": 0, "insatisfecho": 0}
    for s in satisfactions:
        buckets[satisfaction_bucket(s)] += 1
    return buckets


def content_signature(contents: list[str]) -> str:
    """Hash estable del contenido de una conversación, para deduplicar los
    agregados (B4): dos conversaciones byte-idénticas (pruebas de determinismo)
    dan la misma firma y se cuentan una sola vez, aunque tengan external_id distinto."""
    joined = "␟".join(c or "" for c in contents)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
