"""AUD-6.1: SPC — baseline + reglas Western Electric."""

from __future__ import annotations

from app.services.spc import (
    RULE_8_SAME_SIDE,
    RULE_BEYOND_3SIGMA,
    RULE_TREND_6,
    Baseline,
    check_rules,
    compute_baseline,
    detect_drift,
    spc_summary,
)

# Baseline conocido para probar reglas: media 80, σ 5 → UCL 95, LCL 65.
BL = Baseline(mean=80.0, std=5.0, ucl=95.0, lcl=65.0, n=20)


def test_compute_baseline_basic():
    bl = compute_baseline([75, 80, 85, 80, 75, 80, 85, 80], n=8)
    assert bl is not None
    assert 78 <= bl.mean <= 82
    assert bl.ucl > bl.mean > bl.lcl


def test_compute_baseline_too_few():
    assert compute_baseline([80]) is None
    assert compute_baseline([]) is None


def test_rule_beyond_3sigma_low():
    v = check_rules([80, 80, 50], BL)  # 50 < LCL 65
    assert any(x.rule == RULE_BEYOND_3SIGMA and x.direction == "bajo" for x in v)


def test_rule_beyond_3sigma_high():
    v = check_rules([80, 99], BL)  # 99 > UCL 95
    assert any(x.rule == RULE_BEYOND_3SIGMA and x.direction == "alto" for x in v)


def test_rule_8_same_side_below():
    series = [70] * 8  # 8 puntos bajo la media (80), dentro de límites
    v = check_rules(series, BL)
    assert any(x.rule == RULE_8_SAME_SIDE and x.direction == "bajo" for x in v)


def test_rule_trend_6_down():
    series = [90, 88, 86, 84, 82, 80]  # 6 bajando
    v = check_rules(series, BL)
    assert any(x.rule == RULE_TREND_6 and x.direction == "bajo" for x in v)


def test_no_violation_in_control():
    series = [79, 81, 80, 82, 78, 80, 81, 79]  # oscila cerca de la media
    assert check_rules(series, BL) == []


def test_detect_drift_degradacion():
    assert detect_drift([80, 80, 50], BL) == "degradacion"


def test_detect_drift_estable():
    assert detect_drift([79, 81, 80, 82], BL) == "estable"


def test_detect_drift_mejora():
    # 6 puntos subiendo → tendencia alta → mejora
    assert detect_drift([70, 72, 74, 76, 78, 80], BL) == "mejora"


def test_spc_summary_degradacion():
    # 20 puntos estables (~80) como baseline, luego una caída fuera de límites.
    baseline = [79, 81, 80, 82, 78, 80, 81, 79, 80, 82] * 2
    s = spc_summary([*baseline, 40])
    assert s is not None
    assert s.trend == "degradacion"
    assert 79 <= s.baseline.mean <= 81


def test_spc_summary_none_when_insufficient():
    assert spc_summary([80]) is None
