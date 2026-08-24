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


# --- Regresiones del 2026-08-24 -------------------------------------------
# Medido sobre 250 conversaciones del agente de AquaClean: la versión anterior
# marcaba 145 de las 190 del control negativo. Tres clases de falso positivo.

CATALOGO = {
    "productos": {
        "DISPENSER PAPEL HIGIÉNICO JUMBO": 15700,
        "Bolsa KRAFT N5 (100u)": 4000,
        "Lampazo Azul 9": 4100,
        "Jabón en pan Mr Trapo 150gr": 750,
    }
}


def test_total_de_carrito_no_es_alucinacion():
    """3 × $15.700 = $47.100. El total no está en el catálogo: es aritmética."""
    texto = (
        "Agregué 3 unidades de DISPENSER PAPEL HIGIÉNICO JUMBO al carrito. "
        "Resumen: 3 × DISPENSER PAPEL HIGIÉNICO JUMBO ($15.700 c/u) - "
        "**Total: $47.100** (IVA incluido)"
    )
    r = check_prices(texto, CATALOGO)
    assert r.ok is True, f"marcó {r.contradicciones}"


def test_suma_de_productos_distintos_no_es_alucinacion():
    """$15.700 + $4.000 = $19.700, sumando los unitarios citados en el texto."""
    texto = "Llevás DISPENSER ($15.700) y Bolsa KRAFT ($4.000). Total: $19.700"
    r = check_prices(texto, CATALOGO)
    assert r.ok is True, f"marcó {r.contradicciones}"


def test_sku_no_es_precio():
    """DO2260 aportaba '2260' y disparaba veto."""
    texto = "Bolsa KRAFT N5 (100u) - SKU: DO2260 - Precio: $4.000"
    assert 2260 not in stated_prices(texto)
    assert check_prices(texto, CATALOGO).ok is True


def test_timestamp_no_es_precio():
    texto = "Pedido registrado con id 20260823173409, total $15.700"
    assert 20260823173409 not in stated_prices(texto)
    assert check_prices(texto, CATALOGO).ok is True


def test_stock_no_es_precio():
    """'hay 100 unidades en stock' no puede leerse como un precio."""
    assert stated_prices("hay 100 unidades en stock") == []


def test_precio_inventado_sigue_marcandose():
    """El arreglo no puede volver ciego al validador."""
    r = check_prices("Te lo dejo en $23.333 hoy", CATALOGO)
    assert r.ok is False
    assert 23333 in r.contradicciones


def test_multiplo_absurdo_sigue_marcandose():
    """750 × 900 = 675.000 no es un carrito plausible."""
    r = check_prices("El total es $675.000", CATALOGO)
    assert r.ok is False


def test_cifra_pelada_con_palabra_moneda_sigue_contando():
    assert 68000 in stated_prices("cuesta 68000 pesos")
