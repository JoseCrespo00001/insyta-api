"""Validación determinista de CBU / Alias (rúbrica §5, AUD-3.1).

CBU (Clave Bancaria Uniforme, AR): 22 dígitos en dos bloques con dígito
verificador cada uno. No gasta tokens del judge: es checksum puro.

Casos que distingue (§5):
- El bot DA un CBU inválido → falla grave del agente (VETO A5).
- El usuario MANDA un CBU inválido y el bot NO lo detecta → falla del agente.
- El usuario manda un CBU inválido a propósito / comprobante falso → señal de FRAUDE.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Ponderadores estándar del CBU.
_BLOCK1_WEIGHTS = (7, 1, 3, 9, 7, 1, 3)  # sobre los primeros 7 díg del bloque 1
_BLOCK2_WEIGHTS = (3, 9, 7, 1, 3, 9, 7, 1, 3, 9, 7, 1, 3)  # primeros 13 del bloque 2

_CBU_RE = re.compile(r"\b\d{22}\b")
_ALIAS_RE = re.compile(r"^[a-z0-9]+([.-][a-z0-9]+){0,}$")


def _check_digit(digits: str, weights: tuple[int, ...]) -> int:
    s = sum(int(d) * w for d, w in zip(digits, weights, strict=True))
    return (10 - (s % 10)) % 10


def validate_cbu(cbu: str) -> bool:
    """True si el CBU (solo dígitos, 22) tiene ambos verificadores correctos."""
    digits = re.sub(r"\D", "", cbu or "")
    if len(digits) != 22:
        return False
    block1, block2 = digits[:8], digits[8:]
    if _check_digit(block1[:7], _BLOCK1_WEIGHTS) != int(block1[7]):
        return False
    if _check_digit(block2[:13], _BLOCK2_WEIGHTS) != int(block2[13]):
        return False
    return True


def validate_alias(alias: str) -> bool:
    """Alias CBU: 6-20 chars, [a-z0-9] con '.'/'-' como separadores, sin espacios."""
    if not alias:
        return False
    a = alias.strip()
    return 6 <= len(a) <= 20 and bool(_ALIAS_RE.match(a))


def find_cbus(text: str) -> list[str]:
    """Extrae candidatos a CBU (22 dígitos) de un texto."""
    return _CBU_RE.findall(text or "")


@dataclass(frozen=True)
class CbuFinding:
    cbu: str
    valid: bool
    label: str  # ok | agente_cbu_invalido | agente_no_detecta | fraude_comprobante


def classify_cbu(cbu: str, *, sender: str, bot_detected: bool = False) -> CbuFinding:
    """Clasifica un CBU encontrado según quién lo mandó y si es válido (§5).

    sender: "bot" | "user". bot_detected: si el bot marcó el CBU como inválido.
    """
    valid = validate_cbu(cbu)
    if valid:
        return CbuFinding(cbu, True, "ok")
    if sender == "bot":
        # El bot dio un CBU inválido: falla grave (VETO A5).
        return CbuFinding(cbu, False, "agente_cbu_invalido")
    # Usuario mandó un CBU inválido.
    if bot_detected:
        # El bot lo detectó → posible fraude del usuario (comprobante falso).
        return CbuFinding(cbu, False, "fraude_comprobante")
    # El bot no lo detectó → falla del agente.
    return CbuFinding(cbu, False, "agente_no_detecta")


__all__ = [
    "CbuFinding",
    "classify_cbu",
    "find_cbus",
    "validate_alias",
    "validate_cbu",
]
