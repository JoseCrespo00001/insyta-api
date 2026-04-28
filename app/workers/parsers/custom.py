"""Custom SDK CSV parser — same canonical columns as WATI but emitted by the
Insyta SDK directly. Kept as a separate module so the platform identifier
stays consistent end-to-end.
"""

from __future__ import annotations

from collections.abc import Iterator

from app.workers.parsers.base import PARSERS, ConversationDTO
from app.workers.parsers.wati import parse as _wati_parse


def parse(file_bytes: bytes) -> Iterator[ConversationDTO]:
    for conv in _wati_parse(file_bytes):
        conv.platform = "custom_sdk"
        yield conv


PARSERS["custom_sdk"] = parse
