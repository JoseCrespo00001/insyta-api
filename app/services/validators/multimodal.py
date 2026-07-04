"""Detección determinista de multimodal ignorado (rúbrica §C5, AUD-3.4).

Flag si un mensaje es audio/imagen y no tiene media_transcript (el agente no lo
procesó). No gasta tokens del judge.
"""

from __future__ import annotations

from dataclasses import dataclass

_MEDIA_TYPES = frozenset({"audio", "image"})


@dataclass(frozen=True)
class MultimodalFinding:
    turn_id: int
    message_type: str


def find_unprocessed_media(messages: list[dict]) -> list[MultimodalFinding]:
    """Mensajes audio/imagen sin transcripción. `turn_id` = seq si está, si no el índice."""
    out: list[MultimodalFinding] = []
    for i, m in enumerate(messages):
        mtype = (m.get("message_type") or "text").lower()
        if mtype in _MEDIA_TYPES and not (m.get("media_transcript") or "").strip():
            out.append(MultimodalFinding(turn_id=m.get("seq", i), message_type=mtype))
    return out


__all__ = ["MultimodalFinding", "find_unprocessed_media"]
