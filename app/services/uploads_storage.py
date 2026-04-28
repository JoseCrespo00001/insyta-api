"""Persist uploaded CSVs to disk under /tmp.

Supabase Storage integration lands in Sprint 2; until then we write to a local
directory keyed by upload_id so the worker can re-read it without depending on
the request lifetime. The path is returned for the `uploads.storage_path`
column.
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/tmp/insyta-uploads"))


def _ensure_dir() -> Path:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return UPLOAD_DIR


def write_upload(upload_id: uuid.UUID, content: bytes) -> str:
    base = _ensure_dir()
    path = base / f"{upload_id}.csv"
    path.write_bytes(content)
    logger.info("[UPLOADS] Wrote %d bytes to %s", len(content), path)
    return str(path)
