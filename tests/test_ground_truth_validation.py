"""V1 (Prompt 4/4): validación de "Requieren tu intervención" vs el ground truth.

Test de caja negra sobre el golden set etiquetado: la lista de intervención debe
contener EXACTAMENTE las conversaciones críticas. Calcula recall y precisión y falla
ante un falso negativo (una crítica no listada) o un falso positivo.

Qué corre de verdad y qué se estampa del ground truth:
- Los detectores DETERMINISTAS (adversarial + fraude) corren sobre los mensajes reales.
- La severidad por-mensaje del judge se TOMA del ground truth (el judge real es un LLM,
  fuera del gate determinista; su exactitud sobre datos reales es la corrida real del
  golden set + el Δscore v1→v2, trabajo futuro).
- La regla de selección `needs_intervention` es la MISMA que usa el router en prod.
"""

from __future__ import annotations

import csv
from pathlib import Path

from app.services.fraud import detect_fraud, fraud_flag_names
from app.services.report_metrics import needs_intervention
from app.services.validators.adversarial import detect_adversarial

_FIX = Path(__file__).parent / "fixtures"
_CRITICAL = {"alta", "critica"}


def _load_conversations() -> dict[str, list[dict]]:
    turns: dict[str, list[tuple[str, str]]] = {}
    with (_FIX / "samples_demo_tesis.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            turns.setdefault(row["conversation_id"], []).append((row["role"], row["content"]))
    return {
        cid: [{"seq": i, "role": r, "content": c} for i, (r, c) in enumerate(ts)]
        for cid, ts in turns.items()
    }


def _load_ground_truth() -> dict[str, dict]:
    with (_FIX / "samples_demo_ground_truth.csv").open(encoding="utf-8") as f:
        return {row["conversation_id"]: row for row in csv.DictReader(f)}


def _deterministic_signals(msgs: list[dict]) -> tuple[bool, bool]:
    """VETO + problemático desde los detectores deterministas (sin LLM)."""
    adv = detect_adversarial(msgs)
    fraude = fraud_flag_names(detect_fraud(msgs))
    ceded = adv.is_adversarial and adv.bot_ceded  # ataque cedido → VETO firme B1
    has_veto = ceded
    problematic = bool(fraude) or ceded  # == segment "problematico" en prod
    return has_veto, problematic


def _intervenes(msgs: list[dict], gt_row: dict) -> bool:
    has_veto, problematic = _deterministic_signals(msgs)
    has_critical_verdict = gt_row["severidad"] in _CRITICAL  # judge (del ground truth)
    return needs_intervention(
        has_veto=has_veto,
        needs_review=False,
        has_critical_verdict=has_critical_verdict,
        problematic=problematic,
    )


def test_intervention_list_matches_ground_truth_recall_precision(capsys):
    convs = _load_conversations()
    gt = _load_ground_truth()

    expected: set[str] = set()
    actual: set[str] = set()
    for cid, row in gt.items():
        if row["severidad"] in _CRITICAL or row["tipo_problema"] == "adversarial":
            expected.add(cid)
        if _intervenes(convs[cid], row):
            actual.add(cid)

    tp, fn, fp = expected & actual, expected - actual, actual - expected
    recall = len(tp) / len(expected)
    precision = len(tp) / len(actual) if actual else 1.0

    with capsys.disabled():
        print(
            f"\n[V1] Requieren tu intervención vs ground truth · "
            f"recall={recall:.0%} · precisión={precision:.0%} "
            f"(TP={len(tp)} FN={len(fn)} FP={len(fp)} · "
            f"esperadas={len(expected)} listadas={len(actual)})"
        )

    # Aceptación: sin falsos negativos (una crítica que no se listó) ni falsos positivos.
    assert not fn, f"Falso(s) negativo(s) — crítica(s) no listada(s): {sorted(fn)}"
    assert not fp, f"Falso(s) positivo(s) — no-crítica(s) listada(s): {sorted(fp)}"
    assert recall == 1.0
    assert precision == 1.0


def test_deterministic_layer_catches_adversarial_criticals():
    """Los detectores deterministas (sin LLM) enganchan las dos conversaciones
    adversariales críticas — no dependen de la severidad del ground truth."""
    convs = _load_conversations()
    adv_repelled = detect_adversarial(convs["samples-demo-11"])
    adv_ceded = detect_adversarial(convs["samples-demo-12"])
    assert adv_repelled.is_adversarial  # jailbreak/inyección detectado
    assert adv_ceded.is_adversarial and adv_ceded.bot_ceded  # legal cedido → VETO firme


def test_media_severity_conversations_are_not_flagged():
    """Precisión: las conversaciones de severidad media (no críticas) no se listan
    salvo que un detector determinista las marque (no debería)."""
    convs = _load_conversations()
    gt = _load_ground_truth()
    for cid, row in gt.items():
        if row["severidad"] == "media" and row["tipo_problema"] != "adversarial":
            assert not _intervenes(convs[cid], row), f"{cid} (media) no debería listarse"
