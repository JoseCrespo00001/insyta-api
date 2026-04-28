"""CSV parsers per platform.

Each parser exposes `parse(file_bytes: bytes) -> Iterator[ConversationDTO]`
where `ConversationDTO` is a small dataclass shared across platforms. The
processor worker picks the right parser based on the upload's `platform`
field.
"""

from __future__ import annotations

from app.workers.parsers.base import (
    ConversationDTO,
    MessageDTO,
    ParserWarning,
    PARSERS,
    get_parser,
)

# Side-effect imports: each module registers itself in PARSERS on import.
from app.workers.parsers import custom, respondio, wati  # noqa: F401, E402

__all__ = [
    "ConversationDTO",
    "MessageDTO",
    "ParserWarning",
    "PARSERS",
    "get_parser",
]
