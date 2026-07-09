"""Prompt 2/4: sugerencias accionables sin flujo — helpers puros (sin DB/LLM).

Cubre S2 (umbral) y la fidelidad de los counts (se re-derivan de stats, no del LLM).
"""

from __future__ import annotations

from collections import Counter

from app.llm.flow_audit import _normalize_flowless
from app.workers.audit import _build_suggestions, _flowless_stats


def test_flowless_stats_threshold_excludes_otro_includes_fraude():
    counter = Counter({"alucinacion": 5, "otro": 4, "frustracion": 2, "fraude:promesa_pago": 3})
    stats = _flowless_stats(counter, min_messages=3)
    issues = [s["issue_type"] for s in stats]
    assert issues == ["alucinacion", "fraude:promesa_pago"]  # orden desc por count
    assert "otro" not in issues  # excluido aunque count >= 3 (catch-all inaccionable)
    assert "frustracion" not in issues  # count 2 < 3
    fraude = next(s for s in stats if s["issue_type"] == "fraude:promesa_pago")
    assert fraude["label"] == "promesa pago"  # sin prefijo fraude:, _ → espacio


def test_flowless_stats_respects_limit():
    counter = Counter({"a": 9, "b": 8, "c": 7, "d": 6})
    assert len(_flowless_stats(counter, min_messages=1, limit=3)) == 3


def test_normalize_flowless_counts_are_faithful():
    stats = [{"issue_type": "alucinacion", "label": "alucinacion", "count": 5}]
    # El LLM devuelve un número EQUIVOCADO en la evidencia: el count NO debe salir de ahí.
    items = [
        {
            "issue_type": "alucinacion",
            "evidencia": "aparece en 99 mensajes",  # mentira del LLM
            "causa_probable": "el bot inventa promos inexistentes",
            "parche_prompt": "Solo mencioná promos que existan en promos.json.",
            "como_verificar": "re-auditá filtrando issue_type=alucinacion; esperá 0 nuevos",
        }
    ]
    out = _normalize_flowless(items, stats)
    assert len(out) == 1
    s = out[0]
    assert s["count"] == 5  # de stats (fiel), no el 99 del LLM
    assert s["impact"] == "5 mensajes afectados"
    assert s["title"] == "Reducir casos de alucinacion"
    assert {
        "evidencia",
        "causa_probable",
        "parche_prompt",
        "como_verificar",
    } <= s.keys()
    assert s["detail"] == s["causa_probable"]  # front sin actualizar muestra la causa


def test_normalize_flowless_defaults_missing_llm_fields():
    stats = [{"issue_type": "alcance", "label": "alcance", "count": 4}]
    out = _normalize_flowless([], stats)  # el LLM no devolvió nada
    s = out[0]
    assert s["count"] == 4
    assert s["evidencia"] == "" and s["parche_prompt"] == ""
    assert s["detail"]  # cae al texto determinístico, no vacío


def test_build_suggestions_threshold_drops_low_count():
    counter = Counter({"alucinacion": 5, "otro": 1})
    titles = [s["title"] for s in _build_suggestions(counter, min_messages=3)]
    assert any("alucinacion" in t for t in titles)
    assert not any("otro" in t for t in titles)  # 1 < 3 → sin "otro, 1 mensaje"
