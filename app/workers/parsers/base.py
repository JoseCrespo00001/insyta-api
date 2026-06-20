"""Common types for CSV parsers."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable


@dataclass
class MessageDTO:
    role: str  # "user" | "assistant"
    content: str
    timestamp: datetime


@dataclass
class ConversationDTO:
    external_id: str
    platform: str
    started_at: datetime | None = None
    contact_name: str | None = None
    messages: list[MessageDTO] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ParserWarning:
    """Non-fatal issue logged per row (e.g. tz fallback, malformed cell)."""

    row_index: int
    code: str
    detail: str


# Registry filled by individual parser modules at import time.
PARSERS: dict[str, Callable[[bytes], Iterator[ConversationDTO]]] = {}


def get_parser(platform: str) -> Callable[[bytes], Iterator[ConversationDTO]]:
    parser = PARSERS.get(platform)
    if parser is None:
        raise KeyError(f"no parser registered for platform={platform!r}")
    return parser
