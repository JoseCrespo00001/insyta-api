"""Detección de fraude / irregularidades (rúbrica §7, AUD-5.1).

Señales determinsitas (código, sin tokens del judge) sobre los mensajes del
USUARIO. Lo semántico fino (suplantación ambigua, discrepancia imagen/audio) se
puede reforzar con el judge; acá está el núcleo reproducible.

Cada señal devuelve un `FraudFlag` con turn_id + severidad; alimentan
`fraude_flags` de la rúbrica y `update_user_reputation(is_fraud=True)`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.validators.cbu import find_cbus, validate_cbu
from app.services.validators.prices import check_prices, stated_prices

# Palabras clave (AR) para señales determinsitas.
_OFF_CHANNEL_RE = re.compile(
    r"\b(western union|giro|efectivo aparte|por fuera(?: del)?|transferenc\w* directa|"
    r"pag\w+ aparte|mercado ?pago personal|cripto|usdt|binance)\b",
    re.IGNORECASE,
)
_PRICE_PRESSURE_RE = re.compile(
    r"\b(me dijeron que sal\w+|me hac\w+ (un )?descuento|m[aá]s barato|"
    r"me lo dej\w+ en|otro (local|lugar) me lo|precio especial)\b",
    re.IGNORECASE,
)
_THIRD_PARTY_RE = re.compile(
    r"\b(a nombre de otr\w+|datos de (mi|un) (amig\w+|familiar|tercero|conocid\w+)|"
    r"dni de|tarjeta de otr\w+|no es m[ií]o el)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FraudFlag:
    signal: str
    turn_id: int
    severity: str  # baja | media | alta | critica
    detail: str


def _user_msgs(messages: list[dict]) -> list[tuple[int, str]]:
    out = []
    for i, m in enumerate(messages):
        if m.get("role") == "user":
            out.append((m.get("seq", i), m.get("content") or ""))
    return out


def detect_fraud(messages: list[dict], *, precios: object = None) -> list[FraudFlag]:
    """Señales de fraude determinsitas sobre los mensajes del usuario."""
    flags: list[FraudFlag] = []
    invalid_cbu_turns: list[int] = []

    for turn_id, text in _user_msgs(messages):
        # 1. CBU inválido mandado por el usuario.
        for cbu in find_cbus(text):
            if not validate_cbu(cbu):
                invalid_cbu_turns.append(turn_id)

        # 2. Pago fuera del canal oficial.
        if _OFF_CHANNEL_RE.search(text):
            flags.append(
                FraudFlag(
                    "pago_fuera_canal",
                    turn_id,
                    "alta",
                    "Insiste en pago por fuera del canal oficial",
                )
            )

        # 3. Presión por precios/descuentos inexistentes.
        if _PRICE_PRESSURE_RE.search(text):
            sev = "media"
            detail = "Presiona por un precio/descuento no confirmado"
            if precios and stated_prices(text):
                # menciona un monto concreto que no está en la fuente de verdad
                if not check_prices(text, precios).ok:
                    sev = "alta"
                    detail = "Reclama un precio que no coincide con la lista oficial"
            flags.append(FraudFlag("presion_precio", turn_id, sev, detail))

        # 4. Suplantación / datos de terceros.
        if _THIRD_PARTY_RE.search(text):
            flags.append(
                FraudFlag(
                    "suplantacion", turn_id, "media", "Pide/usa datos de terceros"
                )
            )

    # 1b. CBU inválido repetido → señal más fuerte.
    if invalid_cbu_turns:
        sev = "critica" if len(invalid_cbu_turns) >= 2 else "alta"
        flags.append(
            FraudFlag(
                (
                    "cbu_invalido_repetido"
                    if len(invalid_cbu_turns) >= 2
                    else "cbu_invalido"
                ),
                invalid_cbu_turns[0],
                sev,
                f"CBU inválido enviado por el usuario x{len(invalid_cbu_turns)}",
            )
        )

    return flags


def detect_script_pattern(
    user_texts_by_id: dict[str, list[str]], *, min_users: int = 3
) -> list[str]:
    """Patrón de ataque coordinado: mismo texto (normalizado) repetido por >=
    `min_users` external_id distintos. Devuelve los textos sospechosos."""
    seen: dict[str, set[str]] = {}
    for ext_id, texts in user_texts_by_id.items():
        for t in texts:
            norm = re.sub(r"\s+", " ", (t or "").strip().lower())
            if len(norm) < 12:  # ignorá "hola", "si", etc.
                continue
            seen.setdefault(norm, set()).add(ext_id)
    return [norm for norm, ids in seen.items() if len(ids) >= min_users]


def fraud_flag_names(flags: list[FraudFlag]) -> list[str]:
    """Nombres únicos de señales (para el campo fraude_flags de la rúbrica)."""
    return sorted({f.signal for f in flags})


__all__ = [
    "FraudFlag",
    "detect_fraud",
    "detect_script_pattern",
    "fraud_flag_names",
]
