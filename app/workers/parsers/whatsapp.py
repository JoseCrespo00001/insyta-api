"""WhatsApp chat export parser (.txt).

Formato de entrada (multilínea: un mensaje continúa hasta la próxima línea con
corchete de fecha):

    [22/03/2025, 06:11:02] SAMPLES ROPA: ¡Completá tu pedido! 🛒
    (líneas siguientes, incluso en blanco, son parte del mismo mensaje)
    [22/03/2025, 17:06:55] JOCHA: #14763

Todo el archivo es UNA conversación. El remitente del PRIMER mensaje se toma
como el agente/negocio (-> role "assistant"); el resto son el cliente
(-> role "user"). El nombre de la conversación (contact_name) es el cliente.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

from app.workers.parsers.base import PARSERS, ConversationDTO, MessageDTO

logger = logging.getLogger(__name__)

# [DD/MM/YYYY, HH:MM:SS] Remitente: texto   (también acepta DD/MM/YY y H:MM)
_HEADER = re.compile(
    r"^\[(\d{1,2})/(\d{1,2})/(\d{2,4}),?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\]\s+([^:]+?):\s?(.*)$"
)
_ARG_TZ = timezone(timedelta(hours=-3))  # WhatsApp exporta en hora local (AR)


def _parse_dt(
    dd: str, mm: str, yyyy: str, hh: str, mi: str, ss: str | None
) -> datetime:
    year = int(yyyy)
    if year < 100:
        year += 2000
    return datetime(
        year, int(mm), int(dd), int(hh), int(mi), int(ss or 0), tzinfo=_ARG_TZ
    ).astimezone(timezone.utc)


def parse(file_bytes: bytes) -> Iterator[ConversationDTO]:
    text = file_bytes.decode("utf-8-sig", errors="replace")
    raw_msgs: list[tuple[str, datetime, str]] = []  # (sender, ts, content)

    for line in text.splitlines():
        m = _HEADER.match(line)
        if m:
            dd, mm, yyyy, hh, mi, ss, sender, content = m.groups()
            ts = _parse_dt(dd, mm, yyyy, hh, mi, ss)
            raw_msgs.append((sender.strip(), ts, content))
        elif raw_msgs:
            # Continuación del mensaje anterior (multilínea / línea en blanco).
            s, t, c = raw_msgs[-1]
            raw_msgs[-1] = (s, t, (c + "\n" + line).strip())

    if not raw_msgs:
        logger.error(
            "[WHATSAPP] no se detectaron mensajes con formato [fecha] Remitente:"
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
