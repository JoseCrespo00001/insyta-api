"""Cotizaciones de servicio derivables de la tarifa (rúbrica §A1).

La cuenta la hace una función, no el modelo de lenguaje: una cotización es
aritmética sobre la tarifa y no admite criterio.
"""

from __future__ import annotations

from app.services.validators.prices import check_prices
from app.services.validators.quotes import derivables, superficies_mencionadas

TARIFA = {
    "servicios": {
        "residencial": {
            "precio_m2": {"general": 40, "profunda": 60, "especializada": 80},
            "minimo": 2500,
        },
        "post_obra": {
            "precio_m2": {"general": 70, "profunda": 90, "especializada": 120},
            "recargo_escombros": 0.30,
            "minimo": 6000,
        },
    },
    "frecuencia": {"unica": 1.0, "recurrente": 0.85},
}


def test_superficie_en_varios_formatos():
    assert superficies_mencionadas("limpieza de 60 m2") == [60]
    assert superficies_mencionadas("son 110m² en total") == [110]
    assert superficies_mencionadas("80 metros cuadrados") == [80]
    assert superficies_mencionadas("no hay superficie acá") == []


def test_post_obra_con_recargo_de_escombros():
    """70 $/m2 × 110 m2 × 1,30 = 10.010."""
    q = derivables("limpieza post-obra de 110 m2: $10.010", TARIFA)
    assert 10010 in q.derivables


def test_minimo_con_descuento_recurrente():
    """40 × 60 = 2.400, por debajo del mínimo de 2.500; recurrente: × 0,85 = 2.125."""
    q = derivables("residencial 60 m2 recurrente: $2.125", TARIFA)
    assert 2125 in q.derivables


def test_sin_superficie_no_opina():
    q = derivables("te lo dejo en $9.999", TARIFA)
    assert q.derivables == set()


def test_sin_tarifa_de_servicios_no_opina():
    q = derivables("limpieza de 60 m2 por $2.400", {"productos": {"Mopa": 1700}})
    assert q.derivables == set()


def test_cotizacion_correcta_no_dispara_contradiccion():
    texto = "Cotización post-obra, 110 m², polvo fino y escombros: $10.010 (IVA incluido)"
    assert check_prices(texto, TARIFA).ok is True


def test_cotizacion_inventada_sigue_marcandose():
    """7.700 × 1,5 no sale de ninguna combinación de la tarifa."""
    r = check_prices("Cotización post-obra 110 m²: $11.550", TARIFA)
    assert r.ok is False
    assert 11550 in r.contradicciones


def test_enumeracion_acotada():
    """3 intensidades × 2 recargos × 2 frecuencias × 1 superficie, por servicio."""
    q = derivables("110 m2", TARIFA)
    assert q.combinaciones <= 64
