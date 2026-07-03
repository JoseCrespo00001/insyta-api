"""AUD-5.1: señales deterministas de fraude (§7)."""

from __future__ import annotations

from app.services.fraud import (
    detect_fraud,
    detect_script_pattern,
    fraud_flag_names,
)

PRECIOS = {"servicios": {"limpieza": 45000}}
_INVALID_CBU = "1" * 22  # verificadores incorrectos


def _u(seq, text):
    return {"seq": seq, "role": "user", "content": text}


def _b(seq, text):
    return {"seq": seq, "role": "assistant", "content": text}


def test_off_channel_payment_flagged():
    msgs = [_u(0, "mejor te transfiero por western union asi es mas rapido")]
    flags = detect_fraud(msgs)
    assert "pago_fuera_canal" in fraud_flag_names(flags)


def test_price_pressure_flagged():
    msgs = [_u(0, "en otro local me lo dejan mas barato, haceme un descuento")]
    assert "presion_precio" in fraud_flag_names(detect_fraud(msgs))


def test_price_pressure_high_when_contradicts_source():
    msgs = [_u(0, "me dijeron que salia $30.000 la limpieza")]
    flags = detect_fraud(msgs, precios=PRECIOS)
    pf = [f for f in flags if f.signal == "presion_precio"]
    assert pf and pf[0].severity == "alta"


def test_third_party_data_flagged():
    msgs = [_u(0, "necesito los datos de mi amigo para pagar a nombre de otra persona")]
    assert "suplantacion" in fraud_flag_names(detect_fraud(msgs))


def test_invalid_cbu_once():
    msgs = [_u(0, f"te paso el cbu {_INVALID_CBU}")]
    assert "cbu_invalido" in fraud_flag_names(detect_fraud(msgs))


def test_invalid_cbu_repeated_is_critical():
    msgs = [_u(0, f"cbu {_INVALID_CBU}"), _u(1, f"perdon el otro {_INVALID_CBU}")]
    flags = detect_fraud(msgs)
    rep = [f for f in flags if f.signal == "cbu_invalido_repetido"]
    assert rep and rep[0].severity == "critica"


def test_clean_conversation_no_flags():
    msgs = [_u(0, "hola, necesito una limpieza"), _b(1, "hola! con gusto")]
    assert detect_fraud(msgs) == []


def test_bot_messages_not_flagged():
    # La presión/pago fuera de canal solo cuenta en mensajes del usuario.
    msgs = [_b(0, "te dejo un descuento y pagás por western union")]
    assert detect_fraud(msgs) == []


def test_script_pattern_across_users():
    texts = {
        "u1": ["quiero el premio gratis ya mismo por favor"],
        "u2": ["quiero el premio gratis ya mismo por favor"],
        "u3": ["quiero el premio gratis ya mismo por favor"],
        "u4": ["hola necesito limpieza"],
    }
    sus = detect_script_pattern(texts, min_users=3)
    assert any("premio gratis" in s for s in sus)


def test_script_pattern_ignores_short_and_few():
    texts = {"u1": ["hola"], "u2": ["hola"], "u3": ["hola"]}
    assert detect_script_pattern(texts, min_users=3) == []
