"""Respond.io CSV parser.

Respond.io export schema differs slightly from WATI:
    chat_id, sender_type, message, sent_at

Reuses the WATI helpers since the per-row logic is identical once we map the
column names.
"""

from __future__ import annotations

import csv
import io
import logging
from collections import defaultdict
from collections.abc import Iterator

from app.workers.parsers.base import PARSERS, ConversationDTO, MessageDTO
from app.workers.parsers.wati import _normalize_role, _parse_timestamp

logger = logging.getLogger(__name__)


def parse(file_bytes: bytes) -> Iterator[ConversationDTO]:
    text = file_bytes.decode("utf-8-sig", errors="replace")
    buckets: dict[str, ConversationDTO] = defaultdict(
        lambda: ConversationDTO(external_id="", platform="respondio")
    )
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return
    fieldnames = [f.strip().lstrip("﻿").lower() for f in reader.fieldnames]
    reader.fieldnames = fieldnames
    required = {"chat_id", "sender_type", "message", "sent_at"}
    missing = required - set(fieldnames)
    if missing:
        logger.error("[RESPONDIO] missing required columns: %s", sorted(missing))
        return

    for idx, row in enumerate(reader, start=2):
        conv_id = (row.get("chat_id") or "").strip()
        if not conv_id:
            continue
        ts = _parse_timestamp(row.get("sent_at", ""), conv_id=conv_id, row_idx=idx)
        msg = MessageDTO(
            role=_normalize_role(row.get("sender_type", "")),
            content=(row.get("message") or "").strip(),
            timestamp=ts,
        )
        bucket = buckets[conv_id]
        bucket.external_id = conv_id
        bucket.messages.append(msg)
        if bucket.started_at is None or ts < bucket.started_at:
            bucket.started_at = ts

    for conv in buckets.values():
        conv.messages.sort(key=lambda m: m.timestamp)
        yield conv


PARSERS["respondio"] = parse
