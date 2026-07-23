"""WhatsApp chat export parser (.txt).

Acepta los dos dialectos de export (multilínea: un mensaje continúa hasta la
próxima línea con header de fecha):

iOS (corchetes, con segundos):

    [22/03/2025, 06:11:02] SAMPLES ROPA: ¡Completá tu pedido! 🛒
    (líneas siguientes, incluso en blanco, son parte del mismo mensaje)
    [22/03/2025, 17:06:55] JOCHA: #14763

Android (sin corchetes, separador " - ", sin segundos, am/pm según locale):

    22/3/2025 14:05 - Remitente: texto
    3/22/25, 6:11 PM - Sender: text

Android además intercala caracteres invisibles (U+200E/U+200F al inicio de
línea, U+202F antes de AM/PM) que se normalizan antes de matchear, y mensajes
de sistema sin remitente ("Los mensajes y las llamadas están cifrados...") que
se descartan sin romper la continuación multilínea.

Todo el archivo es UNA conversación. El remitente del PRIMER mensaje se toma
como el agente/negocio (-> role "assistant"); el resto son el cliente
(-> role "user"). El nombre de la conversación (contact_name) es el cliente.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone

from app.workers.parsers.base import PARSERS, ConversationDTO, MessageDTO

logger = logging.getLogger(__name__)

# [DD/MM/YYYY, HH:MM:SS] Remitente: texto   (también acepta DD/MM/YY y H:MM)
_HEADER = re.compile(
    r"^\[(\d{1,2})/(\d{1,2})/(\d{2,4}),?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\]\s+([^:]+?):\s?(.*)$"
)

# Dialecto Android: "22/3/2025 14:05 - Remitente: texto" o
# "3/22/25, 6:11 PM - Sender: text". Coma opcional tras la fecha, segundos
# opcionales (algunos builds los incluyen), am/pm opcional en cualquiera de
# sus variantes de locale ("AM", "a. m.", "p.m.", ...). El resto después del
# " - " se separa aparte en _ANDROID_SENDER para poder detectar mensajes de
# sistema sin remitente.
_HEADER_ANDROID = re.compile(
    r"^(\d{1,2})/(\d{1,2})/(\d{2,4}),?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?"
    r"(?:\s*([ap])\.?\s?m\.?)?"
    r"\s+-\s(.*)$",
    re.IGNORECASE,
)
# "Remitente: texto" — si no matchea, la línea es un system message.
_ANDROID_SENDER = re.compile(r"^([^:]+?):\s?(.*)$")

# Android intercala marcas bidi (U+200E/U+200F) y narrow no-break space
# (U+202F, típico antes de AM/PM); se normalizan antes de matchear headers.
_BIDI_MARKS = re.compile("[\\u200e\\u200f]")

_ARG_TZ = timezone(timedelta(hours=-3))  # WhatsApp exporta en hora local (AR)


def _normalize_line(line: str) -> str:
    return _BIDI_MARKS.sub("", line).replace("\u202f", " ")


def _detect_day_first(lines: list[str]) -> bool:
    """Heurística de orden de fecha para los headers Android del archivo.

    Si algún primer campo > 12 → es día (DD/MM); si no, y algún segundo campo
    > 12 → MM/DD; si todo es ambiguo → DD/MM (locale AR/LATAM). Se decide una
    sola vez por archivo para no mezclar interpretaciones entre líneas.
    """
    saw_second_gt_12 = False
    for line in lines:
        m = _HEADER_ANDROID.match(line)
        if not m:
            continue
        if int(m.group(1)) > 12:
            return True
        if int(m.group(2)) > 12:
            saw_second_gt_12 = True
    return not saw_second_gt_12


def _parse_dt(dd: str, mm: str, yyyy: str, hh: str, mi: str, ss: str | None) -> datetime:
    year = int(yyyy)
    if year < 100:
        year += 2000
    return datetime(
        year, int(mm), int(dd), int(hh), int(mi), int(ss or 0), tzinfo=_ARG_TZ
    ).astimezone(UTC)


def _parse_android_dt(
    f1: str,
    f2: str,
    yyyy: str,
    hh: str,
    mi: str,
    ss: str | None,
    ampm: str | None,
    day_first: bool,
) -> datetime:
    """Arma el datetime de un header Android. Levanta ValueError si la fecha
    u hora es inválida (ej. mes 22 con day_first mal detectado)."""
    day, month = (int(f1), int(f2)) if day_first else (int(f2), int(f1))
    year = int(yyyy)
    if year < 100:
        year += 2000
    hour = int(hh)
    if ampm is not None:
        # 12 AM → 00, 12 PM → 12, 1-11 PM → +12.
        hour %= 12
        if ampm.lower() == "p":
            hour += 12
    return datetime(year, month, day, hour, int(mi), int(ss or 0), tzinfo=_ARG_TZ).astimezone(UTC)


def parse(file_bytes: bytes) -> Iterator[ConversationDTO]:
    text = file_bytes.decode("utf-8-sig", errors="replace")
    lines = [_normalize_line(raw) for raw in text.splitlines()]
    day_first = _detect_day_first(lines)

    raw_msgs: list[tuple[str, datetime, str]] = []  # (sender, ts, content)
    skip_continuation = False  # True tras descartar un system message Android

    for line in lines:
        m = _HEADER.match(line)
        if m:
            dd, mm, yyyy, hh, mi, ss, sender, content = m.groups()
            ts = _parse_dt(dd, mm, yyyy, hh, mi, ss)
            raw_msgs.append((sender.strip(), ts, content))
            skip_continuation = False
            continue

        ma = _HEADER_ANDROID.match(line)
        if ma:
            f1, f2, yyyy, hh, mi, ss, ampm, rest = ma.groups()
            ts_android: datetime | None
            try:
                ts_android = _parse_android_dt(f1, f2, yyyy, hh, mi, ss, ampm, day_first)
            except ValueError:
                ts_android = None  # header con fecha inválida → línea común
            if ts_android is not None:
                ms = _ANDROID_SENDER.match(rest)
                if ms is None:
                    # System message sin remitente ("Los mensajes y las
                    # llamadas están cifrados...", cambios de grupo, etc.):
                    # se descarta junto con sus eventuales continuaciones.
                    skip_continuation = True
                else:
                    raw_msgs.append((ms.group(1).strip(), ts_android, ms.group(2)))
                    skip_continuation = False
                continue

        if skip_continuation:
            # Continuación de un system message descartado: también se salta.
            continue
        if raw_msgs:
            # Continuación del mensaje anterior (multilínea / línea en blanco).
            s, t, c = raw_msgs[-1]
            raw_msgs[-1] = (s, t, (c + "\n" + line).strip())

    if not raw_msgs:
        logger.error(
            "[WHATSAPP] no se detectaron mensajes con formato iOS "
            "('[fecha] Remitente:') ni Android ('fecha - Remitente:')"
        )
        return

    agent_sender = raw_msgs[0][0]  # el primer remitente = negocio/agente
    customer = next((s for s, _, _ in raw_msgs if s != agent_sender), agent_sender)

    # external_id estable: si re-subís el mismo chat, dedupea por (agent, external_id).
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    conv = ConversationDTO(
        external_id=f"wa-{digest}",
        platform="whatsapp",
        contact_name=customer,
        started_at=raw_msgs[0][1],
    )
    for sender, ts, content in raw_msgs:
        conv.messages.append(
            MessageDTO(
                role="assistant" if sender == agent_sender else "user",
                content=content,
                timestamp=ts,
            )
        )
    conv.messages.sort(key=lambda m: m.timestamp)
    yield conv


PARSERS["whatsapp"] = parse
