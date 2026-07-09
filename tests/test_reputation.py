"""AUD-4.2: lógica pura de reputación (media móvil, hash, etiqueta)."""

from __future__ import annotations

from app.services.reputation import (
    hash_user_key,
    merge_avg,
    risk_label,
    user_note,
)


def test_user_note_none_when_clean():
    assert user_note(False, 0) is None


def test_user_note_warns_when_risky():
    note = user_note(True, 3)
    assert note is not None
    assert "riesgoso" in note.lower()
    assert "3" in note


def test_merge_avg_first_value():
    assert merge_avg(None, 0, 80) == 80.0


def test_merge_avg_incremental():
    # avg 80 sobre 1 muestra + nueva 60 → 70
    assert merge_avg(80, 1, 60) == 70.0
    # avg 70 sobre 2 + nueva 100 → 80
    assert merge_avg(70, 2, 100) == 80.0


def test_hash_user_key_stable_and_no_plaintext():
    h1 = hash_user_key("+5491122334455")
    h2 = hash_user_key("+5491122334455")
    assert h1 == h2
    assert "5491122334455" not in h1
    assert len(h1) == 64
    assert hash_user_key("otro") != h1


def test_risk_label_problematico_by_fraud():
    assert risk_label(2, 5.0, False) == "problematico"


def test_risk_label_lead():
    assert risk_label(0, 3.0, True) == "lead_calificado"


def test_risk_label_recurrente_by_sentiment():
    assert risk_label(0, 4.5, False) == "recurrente"


def test_risk_label_neutral_default():
    assert risk_label(0, 2.0, False) == "neutral"


# ── Regresión: rep NUEVO no debe explotar con None += int ──────────────────
# El default=0 de SQLAlchemy recién aplica en el flush; al crear el rep e
# incrementar antes de flushear, los contadores eran None → None += 1. Fix:
# inicializarlos en 0 en el constructor.
import uuid as _uuid  # noqa: E402

import pytest  # noqa: E402


class _FakeResult:
    def scalar_one_or_none(self):
        return None


class _FakeSession:
    def __init__(self):
        self.added = None

    async def execute(self, *a, **k):
        return _FakeResult()

    def add(self, obj):
        self.added = obj


@pytest.mark.asyncio
async def test_new_agent_reputation_no_typeerror():
    from app.services.reputation import update_agent_reputation

    s = _FakeSession()
    await update_agent_reputation(
        s,
        agent_id=_uuid.uuid4(),
        org_id=_uuid.uuid4(),
        project_id=_uuid.uuid4(),
        score=80.0,
        has_veto=True,
    )
    assert s.added.score_count == 1
    assert s.added.veto_count == 1


@pytest.mark.asyncio
async def test_new_user_reputation_no_typeerror():
    from app.services.reputation import update_user_reputation

    s = _FakeSession()
    await update_user_reputation(
        s,
        external_id="+5491100000000",
        org_id=_uuid.uuid4(),
        project_id=_uuid.uuid4(),
        sentiment=4.0,
        is_lead=True,
        is_fraud=True,
    )
    assert s.added.sentiment_count == 1
    assert s.added.fraud_attempts == 1
