"""AUD-3.3: contradicción de precios vs la fuente de verdad."""

from __future__ import annotations

from app.services.validators.prices import (
    check_prices,
    flatten_prices,
    stated_prices,
)

PRECIOS = {
    "servicios": {
        "limpieza_profunda": {"50m2": 45000, "80m2": 68000, "120m2": 95000},
        "por_hora": 8000,
    },
    "moneda": "ARS",
}


def test_flatten_collects_all_numbers():
    vals = flatten_prices(PRECIOS)
    assert {45000, 68000, 95000, 8000} <= vals


def test_stated_prices_extraction():
    assert 45000 in stated_prices("sale $45.000 la limpieza")
    assert 68000 in stated_prices("cuesta 68000 pesos")
    # "50m2" no es precio (2 dígitos, sin símbolo de moneda)
    assert 50 not in stated_prices("para 50m2")


def test_correct_price_no_contradiction():
    r = check_prices("La limpieza profunda de 50m2 sale $45.000", PRECIOS)
    assert r.ok is True
    assert r.contradicciones == []


def test_invented_price_flagged():
    r = check_prices("Te lo dejo en $52.000 hoy", PRECIOS)
    assert r.ok is False
    assert 52000 in r.contradicciones


def test_no_price_list_skips_check():
    r = check_prices("sale $999.999", None)
    assert r.ok is True
    assert r.contradicciones == []


def test_tolerance_allows_small_diff():
    r = check_prices("sale $45.010", PRECIOS, tolerance=50)
    assert r.ok is True
