"""AUD-3.2: SLA / latencia / horario deterministas."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.validators.sla import BusinessHours, compute_sla

TZ = timezone.utc
# Miércoles 2026-07-01 10:00 UTC (día hábil, dentro de horario).
BASE = datetime(2026, 7, 1, 10, 0, 0, tzinfo=TZ)


def _m(role: str, offset_s: int):
    return {"role": role, "timestamp": BASE + timedelta(seconds=offset_s)}


def test_first_response_and_resolucion():
    msgs = [
        _m("user", 0),
        _m("assistant", 30),
        _m("user", 100),
        _m("assistant", 140),
    ]
    r = compute_sla(msgs)
    assert r.first_response_s == 30
    assert r.resolucion_s == 140
    # avg de (30, 40) = 35
    assert r.avg_response_s == 35


def test_too_few_messages_all_none():
    r = compute_sla([_m("user", 0)])
    assert r == compute_sla([])  # ambos: todo None
    assert r.first_response_s is None


def test_fuera_horario_false_within():
    msgs = [_m("user", 0), _m("assistant", 60)]
    r = compute_sla(msgs, BusinessHours())
    assert r.fuera_horario is False


def test_fuera_horario_true_night():
    night = datetime(2026, 7, 1, 3, 0, 0, tzinfo=TZ)  # 3am
    msgs = [
        {"role": "user", "timestamp": night},
        {"role": "assistant", "timestamp": night + timedelta(seconds=60)},
    ]
    r = compute_sla(msgs, BusinessHours())
    assert r.fuera_horario is True


def test_fuera_horario_true_weekend():
    sunday = datetime(2026, 7, 5, 11, 0, 0, tzinfo=TZ)  # domingo
    msgs = [
        {"role": "user", "timestamp": sunday},
        {"role": "assistant", "timestamp": sunday + timedelta(seconds=60)},
    ]
    assert compute_sla(msgs, BusinessHours()).fuera_horario is True


def test_fuera_horario_none_without_business_hours():
    msgs = [_m("user", 0), _m("assistant", 60)]
    assert compute_sla(msgs).fuera_horario is None
