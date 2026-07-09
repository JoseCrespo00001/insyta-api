"""V2 (Prompt 4/4): grounding del veredicto — tests puros (sin DB, sin LLM).

Aceptación: un veredicto que cita un string ausente ("LOC_D073") se marca de baja
confianza (severidad bajada + tag + para revisión), en vez de exponerlo como hecho.
"""

from __future__ import annotations

from app.llm.audit_judge import MessageVerdict
from app.services.verdict_grounding import (
    UNVERIFIED_TAG,
    cited_tokens,
    guard_verdicts,
    ungrounded_tokens,
)


def test_cited_tokens_extracts_codes_and_prices():
    note = "El bot ofreció el código BIENVENIDO25 y el pedido MI-88888 a $45.990"
    toks = cited_tokens(note)
    assert "BIENVENIDO25" in toks
    assert "MI-88888" in toks
    assert "$45.990" in toks


def test_cited_tokens_ignores_plain_words():
    # ENVIOGRATIS (solo letras) y DAN no son "dato concreto verificable" acá.
    assert cited_tokens("mencionó ENVIOGRATIS y modo DAN sin más") == []


def test_ungrounded_flags_absent_code():
    note = "El agente citó el local LOC_D073 como disponible"
    message = "hola, tienen local en Córdoba? pasame la dirección"
    sources = '{"precios": {"buzo": 20000}}'
    assert ungrounded_tokens(note, message, sources) == ["LOC_D073"]


def test_grounded_code_present_in_message_not_flagged():
    # Si el bot realmente dijo el código, citarlo NO es alucinación del juez.
    note = "El bot inventó el código NEOTECH25 que no existe"
    message = "usá NEOTECH25 en el checkout"  # el código SÍ aparece en la conversación
    assert ungrounded_tokens(note, message, None) == []


def test_grounded_code_present_in_sources_not_flagged():
    note = "confirmó el precio $20.000 del buzo"
    assert ungrounded_tokens(note, "cuánto sale el buzo?", "buzo canguro $20.000") == []


def test_guard_downgrades_and_marks_hallucinated_verdict():
    verdicts = [
        MessageVerdict(
            seq=3,
            label="error",
            issue_type="alucinacion",
            severity="critica",
            note="El agente afirmó el tracking MI-99999 que no figura en el sistema",
        ),
        MessageVerdict(seq=1, label="ok", issue_type=None, severity=None, note=None),
    ]
    conversation = "che fijate mi pedido cómo viene, decime el tracking"
    adjusted, n_flagged, seqs = guard_verdicts(verdicts, conversation, None)

    assert n_flagged == 1
    assert seqs == [3]
    flagged = adjusted[0]
    # Baja confianza: severidad crítica → media.
    assert flagged.severity == "media"
    # Marcado como no verificado (no se expone el dato como hecho).
    assert UNVERIFIED_TAG in (flagged.note or "")
    assert "MI-99999" in (flagged.note or "")
    # El veredicto ok no se toca.
    assert adjusted[1].severity is None


def test_guard_leaves_grounded_verdict_intact():
    verdicts = [
        MessageVerdict(
            seq=2,
            label="error",
            issue_type="alucinacion",
            severity="alta",
            note="El bot prometió el cupón VERANO70 sin respaldo",
        )
    ]
    # El cupón SÍ aparece en la conversación → el juez no alucinó.
    conversation = "me confirmás el cupón VERANO70 del 70%?"
    adjusted, n_flagged, _ = guard_verdicts(verdicts, conversation, None)
    assert n_flagged == 0
    assert adjusted[0].severity == "alta"  # intacto
    assert UNVERIFIED_TAG not in (adjusted[0].note or "")
