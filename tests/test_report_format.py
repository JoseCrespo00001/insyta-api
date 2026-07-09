"""AUD-7.1: eval_to_camel expone los campos de la rúbrica."""

from __future__ import annotations

from decimal import Decimal

from app.models import Evaluation
from app.services.report_format import eval_to_camel


def test_none_eval_has_rubric_defaults():
    d = eval_to_camel(None)
    assert d["scoreFinal"] is None
    assert d["hasVeto"] is False
    assert d["vetoFlags"] == []
    assert d["segment"] is None
    # Eje adversarial (Prompt 3/4): defaults.
    assert d["isAdversarial"] is False
    assert d["attackType"] is None
    assert d["attackRepelled"] is None
    assert d["vetoConfidence"] is None


def test_eval_exposes_adversarial_fields():
    ev = Evaluation(
        score=85,
        satisfaction=5,
        is_adversarial=True,
        attack_type="manipulacion_legal",
        attack_repelled=False,
        veto_confidence=Decimal("0.90"),
        veto_firm=True,
        has_veto=True,
    )
    d = eval_to_camel(ev)
    assert d["isAdversarial"] is True
    assert d["attackType"] == "manipulacion_legal"
    assert d["attackRepelled"] is False
    assert d["vetoConfidence"] == 0.9
    assert d["vetoFirm"] is True


def test_eval_exposes_rubric_fields():
    ev = Evaluation(
        score=85,
        satisfaction=5,
        score_bruto=90,
        score_final=20,
        confidence=Decimal("0.80"),
        has_veto=True,
        veto_flags=["A2_riesgo_legal"],
        segment="insatisfecho",
        sentiment_trajectory=["neutral", "negativo"],
        requiere_revision_humana=True,
        rubric={"dimensiones": []},
    )
    d = eval_to_camel(ev)
    assert d["scoreBruto"] == 90
    assert d["scoreFinal"] == 20
    assert d["confidence"] == 0.8
    assert d["hasVeto"] is True
    assert d["vetoFlags"] == ["A2_riesgo_legal"]
    assert d["segment"] == "insatisfecho"
    assert d["sentimentTrajectory"] == ["neutral", "negativo"]
    assert d["requiereRevisionHumana"] is True
    assert d["rubric"] == {"dimensiones": []}
