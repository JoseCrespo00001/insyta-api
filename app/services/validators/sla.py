"""SLA / latencia / horario deterministas (rúbrica §D, AUD-3.2).

Se calcula con código a partir de los timestamps de los mensajes — el judge no
gasta tokens en esto. Entrada: lista de mensajes {role, timestamp}.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class BusinessHours:
    start_hour: int = 9  # inclusive
    end_hour: int = 18  # exclusive
    days: frozenset[int] = frozenset({0, 1, 2, 3, 4})  # 0=lunes .. 6=domingo


@dataclass(frozen=True)
class SlaResult:
    first_response_s: float | None
    avg_response_s: float | None
    resolucion_s: float | None
    fuera_horario: bool | None


def _ordered(messages: list[dict]) -> list[dict]:
    msgs = [m for m in messages if m.get("timestamp") is not None]
    return sorted(msgs, key=lambda m: m["timestamp"])


def _is_bot(role: str | None) -> bool:
    return role in ("assistant", "bot", "human_agent")


def compute_sla(
    messages: list[dict], business_hours: BusinessHours | None = None
) -> SlaResult:
    msgs = _ordered(messages)
    if len(msgs) < 2:
        return SlaResult(None, None, None, None)

    ts: list[datetime] = [m["timestamp"] for m in msgs]
    resolucion_s = (ts[-1] - ts[0]).total_seconds()

    # first_response_s: del primer mensaje de usuario a la primera respuesta del bot.
    first_response_s: float | None = None
    first_user_ts: datetime | None = None
    for m in msgs:
        if not _is_bot(m.get("role")) and first_user_ts is None:
            first_user_ts = m["timestamp"]
        elif _is_bot(m.get("role")) and first_user_ts is not None:
            first_response_s = (m["timestamp"] - first_user_ts).total_seconds()
            break

    # avg_response_s: Δt de cada respuesta del bot respecto al mensaje de usuario previo.
    deltas: list[float] = []
    last_user_ts: datetime | None = None
    for m in msgs:
        if not _is_bot(m.get("role")):
            last_user_ts = m["timestamp"]
        elif last_user_ts is not None:
            deltas.append((m["timestamp"] - last_user_ts).total_seconds())
            last_user_ts = None  # una respuesta por turno de usuario
    avg_response_s = sum(deltas) / len(deltas) if deltas else None

    # fuera_horario: alguna respuesta del bot fuera del horario declarado.
    fuera_horario: bool | None = None
    if business_hours is not None:
        fuera_horario = any(
            _out_of_hours(m["timestamp"], business_hours)
            for m in msgs
            if _is_bot(m.get("role"))
        )

    return SlaResult(first_response_s, avg_response_s, resolucion_s, fuera_horario)


def _out_of_hours(when: datetime, bh: BusinessHours) -> bool:
    if when.weekday() not in bh.days:
        return True
    return not (bh.start_hour <= when.hour < bh.end_hour)


__all__ = ["BusinessHours", "SlaResult", "compute_sla"]
