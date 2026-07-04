"""AUD-3.1: validador CBU/alias + clasificación (§5)."""

from __future__ import annotations

from app.services.validators.cbu import (
    _BLOCK1_WEIGHTS,
    _BLOCK2_WEIGHTS,
    _check_digit,
    classify_cbu,
    find_cbus,
    validate_alias,
    validate_cbu,
)


def _valid_cbu() -> str:
    b1 = "0170099"
    b1 += str(_check_digit(b1, _BLOCK1_WEIGHTS))
    b2 = "2200000677973"
    b2 += str(_check_digit(b2, _BLOCK2_WEIGHTS))
    cbu = b1 + b2
    assert len(cbu) == 22
    return cbu


def _invalid_cbu() -> str:
    cbu = _valid_cbu()
    # Rompé el verificador del bloque 1.
    bad = cbu[:7] + str((int(cbu[7]) + 1) % 10) + cbu[8:]
    assert validate_cbu(bad) is False
    return bad


def test_valid_cbu_passes():
    assert validate_cbu(_valid_cbu()) is True


def test_flipped_check_digit_fails():
    cbu = _valid_cbu()
    # Cambiar el verificador del bloque 1 (posición 8) lo invalida.
    bad_digit = str((int(cbu[7]) + 1) % 10)
    bad = cbu[:7] + bad_digit + cbu[8:]
    assert validate_cbu(bad) is False


def test_wrong_length_fails():
    assert validate_cbu("123") is False
    assert validate_cbu("0" * 21) is False


def test_alias_valid_and_invalid():
    assert validate_alias("aqua.clean") is True
    assert validate_alias("aqua-clean-01") is True
    assert validate_alias("ab") is False  # muy corto
    assert validate_alias("con espacio") is False
    assert validate_alias("MAYUS") is False


def test_find_cbus_in_text():
    cbu = _valid_cbu()
    cbus = find_cbus(f"pagá al {cbu} gracias")
    assert cbu in cbus


def test_classify_bot_invalid_is_veto():
    f = classify_cbu(_invalid_cbu(), sender="bot")
    assert f.valid is False
    assert f.label == "agente_cbu_invalido"


def test_classify_user_invalid_undetected_is_agent_fault():
    f = classify_cbu(_invalid_cbu(), sender="user", bot_detected=False)
    assert f.label == "agente_no_detecta"


def test_classify_user_invalid_detected_is_fraud():
    f = classify_cbu(_invalid_cbu(), sender="user", bot_detected=True)
    assert f.label == "fraude_comprobante"


def test_classify_valid_is_ok():
    assert classify_cbu(_valid_cbu(), sender="bot").label == "ok"
