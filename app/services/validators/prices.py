"""Contradicción de precios vs la fuente de verdad (rúbrica §A1, AUD-3.3).

Complementa el grounding del LLM con un chequeo determinista: extrae los montos
que el bot afirma y los compara contra los precios de `attached_data.precios`.
Un monto claramente-precio que NO está en la lista es una contradicción candidata
(alucinación de precio). Conservador para no generar falsos positivos.

Dos correcciones del 2026-08-24, medidas sobre un conjunto de 250 conversaciones
del agente de AquaClean: la versión anterior marcaba 145 de las 190 del control
negativo (76 %). Ver departments/tecnico/HALLAZGO_VETO_PRECIOS.md del repo del TFG.

1. Un número suelto de cuatro o más dígitos NO es un precio. Sin marca de moneda
   —ni símbolo ni palabra, antes o después— se exige la forma con separador de
   miles. Sin esto, `DO2260` aportaba "2260" y un timestamp aportaba
   "20260823173409", y los dos disparaban veto.

2. Un total o subtotal derivado de precios correctos NO es una invención. Si el
   bot dice "3 × $15.700" y "Total: $47.100", el total no está en el catálogo
   porque es aritmética. Se acepta un monto cuando es múltiplo exacto de algún
   precio del catálogo, o suma de los precios válidos citados en el mismo texto.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services.validators.quotes import derivables as _quote_derivables

# Marca de moneda: símbolo o palabra, antes o después del número.
_SYM = r"\$|ars|pesos?"
# Forma con separador de miles: 15.700 · 1,650 · 1.234.567
_MILES = r"\d{1,3}(?:[.,]\d{3})+"
# Cifra pelada: SOLO cuenta si viene con marca de moneda. Se admite desde un
# dígito, porque el catálogo tiene productos de tres cifras ($880) y sin ellos
# el total del carrito quedaba huérfano y se marcaba como inventado.
_PELADA = r"\d+"

_MONEY_RE = re.compile(
    rf"(?:(?P<sym>{_SYM})\s*)?"
    rf"(?P<num>{_MILES}|{_PELADA})"
    rf"(?:\s*(?P<sym_post>{_SYM}))?",
    re.IGNORECASE,
)

# Cuántas unidades de un mismo producto se consideran un carrito plausible.
# Por encima de esto, un "múltiplo" deja de ser evidencia de que el monto derive
# del catálogo y vuelve a ser sospechoso.
_MAX_UNIDADES = 100
# Cuántos sumandos se exploran al reconstruir un total.
_MAX_SUMANDOS = 10


def _normalize(num: str) -> int | None:
    digits = re.sub(r"[.,]", "", num)
    if not digits.isdigit():
        return None
    return int(digits)


def flatten_prices(precios: object) -> set[int]:
    """Junta recursivamente todos los valores numéricos de la lista de precios."""
    out: set[int] = set()
    if isinstance(precios, dict):
        for v in precios.values():
            out |= flatten_prices(v)
    elif isinstance(precios, list):
        for v in precios:
            out |= flatten_prices(v)
    elif isinstance(precios, bool):
        pass
    elif isinstance(precios, (int, float)):
        out.add(round(precios))
    elif isinstance(precios, str):
        n = _normalize(precios)
        if n is not None:
            out.add(n)
    return out


def stated_prices(text: str) -> list[int]:
    """Montos tipo precio que afirma el texto (bot).

    Una cifra pelada de cuatro o más dígitos solo cuenta como precio si viene
    acompañada de una marca de moneda. Sin ese recaudo, los SKU (`DO2260`) y las
    marcas de tiempo (`20260823173409`) entraban como precios afirmados.
    """
    found: list[int] = []
    for m in _MONEY_RE.finditer(text or ""):
        raw = m.group("num")
        n = _normalize(raw)
        if n is None:
            continue
        tiene_moneda = bool(m.group("sym") or m.group("sym_post"))
        tiene_separador = bool(re.search(r"[.,]", raw))
        if not tiene_moneda and not tiene_separador:
            continue
        found.append(n)
    return found


def _derivable(monto: int, validos: list[int]) -> bool:
    """¿El monto se explica por aritmética sobre los unitarios CITADOS?

    Solo se usan los precios que el MISMO texto afirma y que están en el
    catálogo, nunca el catálogo entero. Con 273 productos, permitir "múltiplo de
    cualquier precio del catálogo" vuelve derivable a casi cualquier cifra y el
    validador deja de detectar las alucinaciones reales: medido sobre el conjunto
    de AquaClean, esa versión bajaba el grupo A (alucinación real) de 19 casos
    detectados a 0.

    El caso legítimo siempre trae su evidencia al lado: el resumen de carrito
    enumera los unitarios y después el total.

      * múltiplo exacto de un unitario citado (`n × precio`, n hasta _MAX_UNIDADES);
      * suma de los unitarios citados.
    """
    if not validos:
        return False

    for p in validos:
        if p > 0 and monto % p == 0 and 2 <= monto // p <= _MAX_UNIDADES:
            return True

    # Alcanzabilidad acotada: qué montos se arman sumando los unitarios citados.
    alcanzables = {0}
    for _ in range(_MAX_SUMANDOS):
        nuevos = {
            a + p
            for a in alcanzables
            for p in validos
            if a + p <= monto
        }
        if nuevos <= alcanzables:
            break
        alcanzables |= nuevos
        if monto in alcanzables:
            return True
    return monto in alcanzables


@dataclass(frozen=True)
class PriceCheck:
    contradicciones: list[int] = field(default_factory=list)
    ok: bool = True


def check_prices(bot_text: str, precios: object, *, tolerance: int = 0) -> PriceCheck:
    """Devuelve los precios afirmados por el bot que NO están en la fuente de
    verdad (dentro de `tolerance`) ni se derivan de ella por aritmética.
    Si no hay lista de precios, no chequea."""
    allowed = flatten_prices(precios)
    if not allowed:
        return PriceCheck([], True)

    afirmados = stated_prices(bot_text)
    en_catalogo = [
        p for p in afirmados if any(abs(p - a) <= tolerance for a in allowed)
    ]
    # Cotizaciones de servicio: la cuenta la hace quotes.py, no el modelo.
    cotizables = _quote_derivables(bot_text, precios).derivables

    bad: list[int] = []
    for p in afirmados:
        if any(abs(p - a) <= tolerance for a in allowed):
            continue
        if any(abs(p - c) <= max(tolerance, 1) for c in cotizables):
            continue
        if _derivable(p, en_catalogo):
            continue
        bad.append(p)
    return PriceCheck(bad, len(bad) == 0)


__all__ = ["PriceCheck", "check_prices", "flatten_prices", "stated_prices"]
