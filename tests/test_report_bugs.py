"""Regresión de los 7 bugs objetivos del auditor/reporte (Prompt 1/4).

Cada bug tiene su(s) test(s), como funciones puras (rápidas, sin DB):

- B1: el score visible se topea por VETO (no muestra el crudo).
- B2: la satisfacción se cuenta con UNA sola regla (summary y chips coinciden).
- B3: `failing` sale con un motivo en lenguaje humano.
- B4: los duplicados de determinismo no inflan los agregados.
- B5: el identificador del cliente es pseudónimo, nunca el teléfono crudo.
- B6: el criterio de resolución (pregunta abierta final = NO resuelto) está en el prompt.
- B7: VETO firme (DET o confianza alta → topea) vs tentativo (LLM confianza baja → no topea).
"""

from __future__ import annotations

from types import SimpleNamespace

from app.llm.prompts.eval_v1 import SYSTEM_PROMPT_V1
from app.llm.schemas import RubricResponse
from app.routers.audits import _human_reason
from app.services.report_metrics import (
    content_signature,
    is_phone_like,
    satisfaction_bucket,
    satisfaction_distribution,
    visible_score,
)
from app.services.reputation import client_pseudonym, hash_user_key
from app.services.rubric_scoring import VETO_CAP, compute_scores


def _rubric(dims, veto=None, confidence=0.9):
    return RubricResponse.model_validate(
        {
            "dimensiones": [
                {"id": i, "score": s, "turn_id": t, "justificacion": "x"} for (i, s, t) in dims
            ],
            "veto_flags": veto or [],
            "confidence": confidence,
        }
    )


# --- B1: score visible topeado por VETO ------------------------------------
def test_b1_visible_score_uses_capped_when_present():
    # score crudo 60 pero score_final topeado a 20 → el visible es 20 (no el 60).
    assert visible_score(60, 20) == 20


def test_b1_visible_score_falls_back_to_raw_without_cap():
    # Sin rúbrica/score_final → cae al crudo del eval.
    assert visible_score(55, None) == 55


def test_b1_firm_veto_makes_visible_score_capped():
    r = _rubric([("A1", 5, 1), ("C1", 5, 2)], veto=["A2_riesgo_legal"], confidence=0.9)
    res = compute_scores(r)
    assert res.score_bruto == 100
    assert visible_score(res.score_bruto, res.score_final) == VETO_CAP == 20


# --- B2: satisfacción, una sola fuente -------------------------------------
def test_b2_satisfaction_distribution_single_rule():
    # 5,4 → satisfecho · 3 → neutral · 1,2/None → insatisfecho.
    dist = satisfaction_distribution([5, 4, 3, 1, None])
    assert dist == {"satisfecho": 2, "neutral": 1, "insatisfecho": 2}


def test_b2_per_conv_bucket_matches_aggregate():
    # El bucket por conversación y el agregado usan la MISMA regla → no se
    # contradicen (era el bug: summary Satisfecho 1 vs chips Satisfecho 3).
    sats = [5, 5, 3, 2]
    aggregate = satisfaction_distribution(sats)
    per_conv = {"satisfecho": 0, "neutral": 0, "insatisfecho": 0}
    for s in sats:
        per_conv[satisfaction_bucket(s)] += 1
    assert aggregate == per_conv


# --- B3: failing con motivo humano -----------------------------------------
def test_b3_human_reason_firm_veto():
    ev = SimpleNamespace(
        has_veto=True,
        veto_flags=["A5_cbu_invalido"],
        rubric={"veto_firm": True},
        requiere_revision_humana=False,
        segment="ok",
    )
    assert _human_reason(ev, []) == "CBU inválido"


def test_b3_human_reason_tentative_veto_is_flagged():
    ev = SimpleNamespace(
        has_veto=True,
        veto_flags=["A1_alucinacion"],
        rubric={"veto_firm": False},
        requiere_revision_humana=False,
        segment="ok",
    )
    assert _human_reason(ev, []) == "Alucinación factual (a confirmar)"


def test_b3_human_reason_from_severity_when_no_veto():
    ev = SimpleNamespace(
        has_veto=False,
        veto_flags=[],
        rubric={},
        requiere_revision_humana=False,
        segment="ok",
    )
    verdicts = [
        {"severity": "critica", "issueType": "alucinacion"},
        {"severity": "alta", "issueType": "alucinacion"},
    ]
    assert _human_reason(ev, verdicts) == "2 Alucinación (critica)"


def test_b3_human_reason_none_when_clean():
    ev = SimpleNamespace(
        has_veto=False,
        veto_flags=[],
        rubric={},
        requiere_revision_humana=False,
        segment="ok",
    )
    assert _human_reason(ev, []) is None


# --- B4: dedup de duplicados de determinismo -------------------------------
def test_b4_identical_content_same_signature():
    assert content_signature(["hola", "que tal"]) == content_signature(["hola", "que tal"])


def test_b4_different_content_different_signature():
    assert content_signature(["hola"]) != content_signature(["chau"])


def test_b4_duplicate_counted_once_in_aggregate():
    # 3 conversaciones, 2 byte-idénticas (prueba de determinismo) → el agregado
    # cuenta 2 firmas, no 3 (era el bug: "22 mensajes con alucinación" inflado).
    convs = [["msg-a"], ["msg-a"], ["msg-b"]]
    seen: set[str] = set()
    counted = 0
    for c in convs:
        sig = content_signature(c)
        if sig in seen:
            continue
        seen.add(sig)
        counted += 1
    assert counted == 2


# --- B5: pseudónimo, sin PII -----------------------------------------------
def test_b5_pseudonym_stable_and_hides_phone():
    phone = "+5491133334444"
    p = client_pseudonym(phone)
    assert p.startswith("cliente #")
    assert phone not in p
    assert "+549" not in p
    assert client_pseudonym(phone) == p  # estable entre llamadas


def test_b5_is_phone_like_detects_pii():
    assert is_phone_like("+5491133334444")
    assert is_phone_like("11 3333-4444")
    assert not is_phone_like("Juan Pérez")


def test_b5_export_row_has_no_raw_phone():
    phone = "+5491133334444"
    row = [client_pseudonym(phone), hash_user_key(phone)]
    assert not any("+549" in str(cell) for cell in row)


# --- B6: criterio de resolución documentado en el prompt -------------------
def test_b6_resolution_criterion_in_prompt():
    # El caso del jogger (cierra con "¿le damos?") deja de contar como resuelto:
    # el criterio queda escrito y byte-estable en el system prompt del judge.
    assert "pregunta abierta" in SYSTEM_PROMPT_V1
    assert "¿le damos?" in SYSTEM_PROMPT_V1


# --- B7: VETO firme vs tentativo -------------------------------------------
def test_b7_det_veto_is_firm_even_with_low_confidence():
    # CBU inválido (validador determinista) → firme aunque el LLM dude → topea.
    r = _rubric([("C1", 5, 1)], confidence=0.2)
    res = compute_scores(r, det_veto=["A5_cbu_invalido"])
    assert res.has_veto is True
    assert res.veto_firm is True
    assert res.score_final == VETO_CAP


def test_b7_llm_veto_low_confidence_is_tentative_and_does_not_cap():
    # VETO del LLM con confianza < umbral → tentativo: NO topea el score.
    r = _rubric([("C1", 5, 1)], veto=["A1_alucinacion"], confidence=0.3)
    res = compute_scores(r, confidence_threshold=0.6)
    assert res.has_veto is True
    assert res.veto_firm is False
    assert res.score_final == res.score_bruto


def test_b7_llm_veto_high_confidence_is_firm_and_caps():
    r = _rubric([("C1", 5, 1)], veto=["A1_alucinacion"], confidence=0.9)
    res = compute_scores(r, confidence_threshold=0.6)
    assert res.veto_firm is True
    assert res.score_final == VETO_CAP


def test_b7_alucinacion_severity_drives_firmness_via_mapping():
    # El pipeline real: una alucinación por-mensaje de severidad baja/media es
    # tentativa (no topea); una alta/critica es firme (topea). Sin severidad
    # (data vieja) → firme, para no bajar la guardia por back-compat.
    from app.llm.schemas import EvaluationResponse
    from app.services.rubric_mapping import map_eval_to_rubric

    base_eval = EvaluationResponse.model_validate(
        {
            "score": 80,
            "resolution": True,
            "satisfaction": 4,
            "tone": "positive",
            "frustration": False,
            "escalated": False,
            "efficiency": 4,
            "scope_violation": False,
            "topic": "x",
            "summary": "s",
        }
    )

    def _verdict(sev):
        return SimpleNamespace(seq=2, issue_type="alucinacion", severity=sev)

    soft = compute_scores(map_eval_to_rubric(base_eval, [_verdict("media")], last_turn=2))
    assert soft.has_veto is True and soft.veto_firm is False
    assert soft.score_final == soft.score_bruto  # tentativo: no topea

    hard = compute_scores(map_eval_to_rubric(base_eval, [_verdict("critica")], last_turn=2))
    assert hard.veto_firm is True and hard.score_final == VETO_CAP
