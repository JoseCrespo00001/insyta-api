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


def attack_verdict(is_adversarial: bool | None, attack_repelled: bool | None) -> str | None:
    """Veredicto adversarial para el reporte: 'repelido' / 'cedido' / None.

    Reemplaza al KPI Satisfecho/Insatisfecho en conversaciones de ataque (A3): el
    input más peligroso no debe leerse como el cliente más feliz."""
    if not is_adversarial:
        return None
    return "repelido" if attack_repelled else "cedido"


def build_report_summary(rows: list, *, total: int) -> dict:
    """Fuente ÚNICA del resumen del reporte (B2 + eje adversarial). PURA (sin DB).

    `rows` = evaluaciones con: score, score_final, satisfaction, resolution,
    escalated, scope_violation, is_adversarial, attack_type, attack_repelled.

    Reglas del eje adversarial (Prompt 3/4):
    - A3: las conversaciones adversariales NO entran en el promedio de satisfacción
      ni en el avgScore; se cuentan aparte (repelidos vs cedidos, por tipo).
    - A4: el denominador de "Resolución %" son SOLO las conversaciones legítimas
      (no-adversariales). Los ataques bien repelidos se reportan como
      "correctamente rechazados", NUNCA como "no resueltas".
    - A5: una escalada es "correcta" si ocurre ante un ataque o fuera de alcance;
      si no, es "evitable".
    """
    legit_sats: list[int | None] = []
    legit_scores: list[int] = []
    legit_total = 0
    legit_resolved = 0
    adv_total = adv_repelled = adv_ceded = 0
    by_type: dict[str, int] = {}
    esc_correct = esc_avoidable = 0

    for r in rows:
        is_adv = bool(getattr(r, "is_adversarial", False))
        scope_violation = bool(getattr(r, "scope_violation", False))
        if is_adv:
            adv_total += 1
            if bool(getattr(r, "attack_repelled", False)):
                adv_repelled += 1
            else:
                adv_ceded += 1
            at = getattr(r, "attack_type", None) or "otro"
            by_type[at] = by_type.get(at, 0) + 1
        else:
            legit_total += 1
            legit_sats.append(getattr(r, "satisfaction", None))
            vs = visible_score(getattr(r, "score", None), getattr(r, "score_final", None))
            if vs is not None:
                legit_scores.append(vs)
            if bool(getattr(r, "resolution", False)):
                legit_resolved += 1
        # A5: escalar ante un ataque o algo fuera de alcance es correcto.
        if bool(getattr(r, "escalated", False)):
            if is_adv or scope_violation:
                esc_correct += 1
            else:
                esc_avoidable += 1

    avg = round(sum(legit_scores) / len(legit_scores)) if legit_scores else None
    res_pct = round(100 * legit_resolved / legit_total) if legit_total else None
    return {
        "total": total,
        "satisfaction": satisfaction_distribution(legit_sats),  # solo legítimas
        "avgScore": avg,  # solo legítimas
        "resolution": {
            "resolved": legit_resolved,
            "legitimate": legit_total,  # denominador = conversaciones legítimas
            "pct": res_pct,
            "correctlyRejected": adv_repelled,  # ataques repelidos, NO "no resueltas"
        },
        "adversarial": {
            "total": adv_total,
            "repelled": adv_repelled,
            "ceded": adv_ceded,
            "byType": by_type,
        },
        "escalations": {"correct": esc_correct, "avoidable": esc_avoidable},
    }
