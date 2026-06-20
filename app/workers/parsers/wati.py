"""WATI CSV parser.

Expected columns:
    conversation_id, role, content, timestamp

Headers may have a UTF-8 BOM. Timestamps may be ISO 8601 with or without
timezone; mixed timezones are tolerated (warning logged, value coerced to
UTC). Emoji in content (4-byte UTF-8) is preserved verbatim.
"""

from __future__ import annotations

import csv
import io
import logging
from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime, timezone

from app.workers.parsers.base import PARSERS, ConversationDTO, MessageDTO

logger = logging.getLogger(__name__)


def _parse_timestamp(raw: str, *, conv_id: str, row_idx: int) -> datetime:
    raw = raw.strip()
    if not raw:
        logger.warning(
            "[WATI] empty timestamp on conv=%s row=%d, defaulting to epoch",
            conv_id,
            row_idx,
        )
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        logger.warning(
            "[WATI] unparseable timestamp %r on conv=%s row=%d, using epoch",
            raw,
            conv_id,
            row_idx,
        )
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    if ts.tzinfo is None:
        # Naive — assume UTC, log so we can spot data sources that need fixing.
        logger.warning(
            "[WATI] naive timestamp on conv=%s row=%d, assuming UTC", conv_id, row_idx
        )
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _normalize_role(raw: str) -> str:
    raw = (raw or "").strip().lower()
    if raw in {"user", "customer", "client", "cliente", "u"}:
        return "user"
    if raw in {"bot", "agent", "assistant", "a"}:
        return "assistant"
    if raw in {"system", "s"}:
        return "system"
    return "user"  # safest default


def parse(file_bytes: bytes) -> Iterator[ConversationDTO]:
    text = file_bytes.decode("utf-8-sig", errors="replace")  # strips BOM
    buckets: dict[str, ConversationDTO] = defaultdict(
        lambda: ConversationDTO(external_id="", platform="wati")
    )
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return
    fieldnames = [f.strip().lstrip("﻿").lower() for f in reader.fieldnames]
    reader.fieldnames = fieldnames
    required = {"conversation_id", "role", "content", "timestamp"}
    missing = required - set(fieldnames)
    if missing:
        logger.error("[WATI] missing required columns: %s", sorted(missing))
        return

    for idx, row in enumerate(reader, start=2):  # +1 for header
        conv_id = (row.get("conversation_id") or "").strip()
        if not conv_id:
            continue
        ts = _parse_timestamp(row.get("timestamp", ""), conv_id=conv_id, row_idx=idx)
        msg = MessageDTO(
            role=_normalize_role(row.get("role", "")),
            content=(row.get("content") or "").strip(),
            timestamp=ts,
        )
        bucket = buckets[conv_id]
        bucket.external_id = conv_id
        # Optional conversation name (first non-empty wins).
        name = (row.get("contact_name") or "").strip()
        if name and not bucket.contact_name:
            bucket.contact_name = name
        bucket.messages.append(msg)
        if bucket.started_at is None or ts < bucket.started_at:
            bucket.started_at = ts

    for conv in buckets.values():
        conv.messages.sort(key=lambda m: m.timestamp)
        yield conv


PARSERS["wati"] = parse
