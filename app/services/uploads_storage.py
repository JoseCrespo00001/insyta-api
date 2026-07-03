"""Persistencia de CSVs subidos en Supabase Storage (bucket privado).

El contenedor `web` sube el CSV crudo a un bucket privado de Supabase Storage y
guarda la object key en `uploads.storage_path`. El worker de Celery (contenedor
separado) lo baja por esa key. Esto elimina la dependencia previa del disco
local, que se rompía entre contenedores porque `web` y `worker` no comparten
filesystem.

Se usa el service-role key (`SUPABASE_SERVICE_KEY`), que bypassa las policies de
Storage — el bucket debe crearse como privado en el proyecto de Supabase.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from functools import lru_cache

from app.core.config import get_settings
from supabase import Client, create_client

logger = logging.getLogger(__name__)

UPLOAD_BUCKET = "uploads"


@lru_cache
def _client() -> Client:
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_key:
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_SERVICE_KEY no configurados: son requeridos "
            "para guardar uploads en Supabase Storage."
        )
    return create_client(settings.supabase_url, settings.supabase_service_key)


def _object_key(upload_id: uuid.UUID) -> str:
    return f"{upload_id}.csv"


def _upload_sync(key: str, content: bytes) -> None:
    _client().storage.from_(UPLOAD_BUCKET).upload(
        path=key,
        file=content,
        file_options={"content-type": "text/csv", "upsert": "true"},
    )


def _download_sync(key: str) -> bytes:
    return _client().storage.from_(UPLOAD_BUCKET).download(key)


def _remove_sync(key: str) -> None:
    _client().storage.from_(UPLOAD_BUCKET).remove([key])


async def write_upload(upload_id: uuid.UUID, content: bytes) -> str:
    """Sube el CSV al bucket y devuelve la object key (va en `uploads.storage_path`)."""
    key = _object_key(upload_id)
    await asyncio.to_thread(_upload_sync, key, content)
    logger.info("[UPLOADS] Subido %d bytes a supabase://%s/%s", len(content), UPLOAD_BUCKET, key)
    return key


async def read_upload(storage_path: str) -> bytes:
    """Baja el CSV del bucket por su object key."""
    return await asyncio.to_thread(_download_sync, storage_path)


async def delete_upload_blob(storage_path: str) -> None:
    """Borra el objeto del bucket (best-effort — el caller ya hizo el soft-delete)."""
    await asyncio.to_thread(_remove_sync, storage_path)
