"""Cotizaciones de servicio derivables de la tarifa (rúbrica §A1).

Complementa a `prices.py`. Ese módulo valida montos contra una lista de precios;
sirve para productos de catálogo, donde el precio correcto es un valor de la
lista. No sirve para servicios, cuyo precio es el resultado de una cuenta:

    total = precio_m2[intensidad] × superficie × (1 + recargos) × frecuencia
    total = max(total, mínimo)

Medido sobre 250 conversaciones del agente de AquaClean, las cotizaciones eran
la única clase de falso positivo que quedaba después de arreglar `prices.py`:
18 de 190 del control negativo. El agente calculaba bien y el validador no tenía
cómo saberlo, porque el resultado no está en ninguna lista.

La cuenta la hace este módulo, no el modelo de lenguaje. Es la misma razón por
la que el chequeo de precios es determinista: un evaluador probabilístico puede
dar por buena una multiplicación mal hecha, o marcar como inventada una bien
hecha, y en los dos casos el veredicto deja de ser reproducible.

Estructura esperada dentro de `attached_data["precios"]`:

    servicios:
      <tipo>:
        precio_m2: {<intensidad>: <valor>, ...}
        minimo: <valor>              (opcional)
        recargo_<algo>: <fracción>   (opcional, 0.30 = 30 %)
    frecuencia:
      <nombre>: <multiplicador>      (opcional, 0.85 = 15 % off)

Si la fuente de verdad no trae `servicios`, el módulo no opina.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import product as _product

# Superficie mencionada en el texto: "60 m2", "110m²", "80 metros cuadrados".
_SUPERFICIE_RE = re.compile(
    r"(\d{1,5}(?:[.,]\d{1,2})?)\s*(?:m\s*[²2]\b|metros?\s+cuadrados?)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class QuoteCheck:
    derivables: set[int] = field(default_factory=set)
    superficies: list[int] = field(default_factory=list)
    combinaciones: int = 0


def superficies_mencionadas(text: str) -> list[int]:
    """Metros cuadrados que el texto menciona, redondeados."""
    out: list[int] = []
    for m in _SUPERFICIE_RE.finditer(text or ""):
        crudo = m.group(1).replace(",", ".")
        try:
            v = round(float(crudo))
        except ValueError:
            continue
        if 0 < v <= 100_000:
            out.append(v)
    return sorted(set(out))


def _tarifas(precios: object) -> dict:
    if isinstance(precios, dict):
        s = precios.get("servicios")
        if isinstance(s, dict):
            return s
    return {}


def _frecuencias(precios: object) -> list[float]:
    out = [1.0]
    if isinstance(precios, dict):
        f = precios.get("frecuencia")
        if isinstance(f, dict):
            for v in f.values():
                if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= 2:
                    out.append(float(v))
    return sorted(set(out))


def _recargos(servicio: dict) -> list[float]:
    """Multiplicadores por recargo: sin recargo (1.0) y con cada uno declarado."""
    out = [1.0]
    for k, v in servicio.items():
        if not k.startswith("recargo_"):
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 5:
            out.append(1.0 + float(v))
    return sorted(set(out))


def derivables(text: str, precios: object) -> QuoteCheck:
    """Montos que se explican como una cotización sobre las superficies del texto.

    Se enumeran todas las combinaciones de tipo de servicio × intensidad ×
    recargo × frecuencia × superficie mencionada, y se aplica el mínimo cuando
    el servicio lo declara. Es una enumeración chica y acotada: con la tarifa de
    AquaClean son 3 tipos × 3 intensidades × ≤2 recargos × 2 frecuencias.
    """
    tarifas = _tarifas(precios)
    sups = superficies_mencionadas(text)
    if not tarifas or not sups:
        return QuoteCheck(set(), sups, 0)

    frecuencias = _frecuencias(precios)
    out: set[int] = set()
    combinaciones = 0

    for servicio in tarifas.values():
        if not isinstance(servicio, dict):
            continue
        por_m2 = servicio.get("precio_m2")
        if not isinstance(por_m2, dict):
            continue
        minimo = servicio.get("minimo")
        minimo = float(minimo) if isinstance(minimo, (int, float)) and not isinstance(minimo, bool) else 0.0

        for tarifa, recargo, frec, sup in _product(
            [v for v in por_m2.values() if isinstance(v, (int, float)) and not isinstance(v, bool)],
            _recargos(servicio),
            frecuencias,
            sups,
        ):
            combinaciones += 1
            bruto = float(tarifa) * sup * recargo
            # El mínimo se aplica antes y después del descuento por frecuencia:
            # las dos lecturas del pliego son defendibles y no conviene marcar
            # una cotización por haber elegido la otra.
            out.add(round(max(bruto, minimo) * frec))
            out.add(round(max(bruto * frec, minimo)))

    return QuoteCheck(out, sups, combinaciones)


__all__ = ["QuoteCheck", "derivables", "superficies_mencionadas"]
