"""Anonymizer tests (EQUIP-63).

Regex layer is exercised explicitly. Presidio NER is skipped if the spaCy
Spanish model isn't installed — these tests stay green in CI without it.
"""

from __future__ import annotations

import re

import pytest

from app.services.anonymizer import anonymize


def _has_phone(text: str) -> bool:
    return bool(re.search(r"\+?54", text)) or bool(
        re.search(r"\d{4}[\s\-]?\d{4}", text)
    )


class TestRegexLayer:
    def test_email_redacted(self):
        result = anonymize("Mi mail es juan@gmail.com")
        assert "juan@gmail.com" not in result.text
        assert "[EMAIL_" in result.text
        # Reverse maps back.
        assert "juan@gmail.com" in result.replacements.values()

    def test_phone_ar_redacted(self):
        result = anonymize("llamame al +54 11 5555 1234")
        assert not _has_phone(result.text)
        assert "[PHONE_" in result.text

    def test_phone_without_country_code(self):
        result = anonymize("mi numero es 11 4567 8901")
        assert "11 4567 8901" not in result.text
        assert "[PHONE_" in result.text

    def test_credit_card_redacted(self):
        result = anonymize("mi tarjeta es 4111 1111 1111 1111")
        assert "4111" not in result.text
        assert "[CARD_" in result.text

    def test_dni_redacted(self):
        result = anonymize("DNI: 12.345.678 — gracias")
        assert "12.345.678" not in result.text
        assert any(token.startswith("[DNI_") for token in result.replacements)

    def test_no_pii_no_change(self):
        result = anonymize("hola, quiero saber el estado de mi pedido 4521")
        assert result.text == "hola, quiero saber el estado de mi pedido 4521"
        assert result.replacements == {}

    def test_order_number_not_redacted(self):
        result = anonymize("mi pedido es 4521")
        assert "4521" in result.text

    def test_empty_input(self):
        result = anonymize("")
        assert result.text == ""
        assert result.replacements == {}

    def test_token_is_stable_across_calls(self):
        a = anonymize("contactame en juan@gmail.com")
        b = anonymize("escribime a juan@gmail.com")
        # Same email -> same token (stable SHA1 suffix).
        a_token = next(t for t in a.replacements if t.startswith("[EMAIL"))
        b_token = next(t for t in b.replacements if t.startswith("[EMAIL"))
        assert a_token == b_token


class TestReverse:
    def test_reverse_round_trip(self):
        original = "soy juan@gmail.com, tel +54 9 11 5555 1234"
        result = anonymize(original)
        recovered = result.reverse(result.text)
        assert recovered == original


class TestPresidioOptional:
    """These tests run only if Presidio + spaCy es model are installed.

    They are not strict requirements for unit-test green; they verify NER works
    when available so the pipeline catches names regex misses.
    """

    def setup_method(self):
        from app.services.anonymizer import _get_presidio

        if _get_presidio() is None:
            pytest.skip("Presidio/spaCy es model not installed")

    def test_person_name_redacted(self):
        result = anonymize("Hola soy Juan Perez de Buenos Aires.")
        assert "Juan Perez" not in result.text
        # Either NAME or LOC token appears.
        assert any(
            t.startswith("[NAME_") or t.startswith("[LOC_") for t in result.replacements
        )
