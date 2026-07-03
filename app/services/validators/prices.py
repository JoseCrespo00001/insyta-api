"""Contradicción de precios vs la fuente de verdad (rúbrica §A1, AUD-3.3).

Complementa el grounding del LLM con un chequeo determinista: extrae los montos
que el bot afirma y los compara contra los precios de `attached_data.precios`.
Un monto claramente-precio que NO está en la lista es una contradicción candidata
(alucinación de precio). Conservador para no generar falsos positivos.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Monto tipo precio: opcional $, dígitos con . o , como separador de miles.
# Requiere contexto de precio ($ o palabra "peso(s)"/"ARS") o >= 4 dígitos, para
# no confundir con m2, cantidades, teléfonos, etc.
_MONEY_RE = re.compile(
    r"(?:(?P<sym>\$|ars|pesos?)\s*)?(?P<num>\d{1,3}(?:[.,]\d{3})+|\d{4,})",
    re.IGNORECASE,
)


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
    """Montos tipo precio que afirma el texto (bot)."""
    found: list[int] = []
    for m in _MONEY_RE.finditer(text or ""):
        # Sin símbolo/palabra de moneda, exigí >= 4 dígitos para reducir ruido.
        raw = m.group("num")
        n = _normalize(raw)
        if n is None:
            continue
        if not m.group("sym") and len(re.sub(r"\D", "", raw)) < 4:
            continue
        found.append(n)
    return found


@dataclass(frozen=True)
class PriceCheck:
    contradicciones: list[int] = field(default_factory=list)
    ok: bool = True


def check_prices(bot_text: str, precios: object, *, tolerance: int = 0) -> PriceCheck:
    """Devuelve los precios afirmados por el bot que NO están en la fuente de
    verdad (dentro de `tolerance`). Si no hay lista de precios, no chequea."""
    allowed = flatten_prices(precios)
    if not allowed:
        return PriceCheck([], True)
    bad: list[int] = []
    for p in stated_prices(bot_text):
        if not any(abs(p - a) <= tolerance for a in allowed):
            bad.append(p)
    return PriceCheck(bad, len(bad) == 0)


__all__ = ["PriceCheck", "check_prices", "flatten_prices", "stated_prices"]
