"""Tests para services/uploads_storage.py — persistencia de CSVs en Supabase Storage.

Antes escribía a disco local (se rompía entre contenedores web/worker); ahora
sube/baja de un bucket privado de Supabase Storage. Estos tests mockean el
cliente Supabase y prueban el roundtrip write→read→delete + el error-path de
credenciales faltantes.
"""

from __future__ import annotations

import uuid

import pytest

from app.services import uploads_storage
from app.services.uploads_storage import (
    delete_upload_blob,
    read_upload,
    write_upload,
)


class _FakeBucket:
    def __init__(self, store: dict[str, bytes]) -> None:
        self.store = store

    def upload(self, path, file, file_options=None):
        self.store[path] = file

    def download(self, path):
        return self.store[path]

    def remove(self, paths):
        for p in paths:
            self.store.pop(p, None)
        return []


class _FakeStorage:
    def __init__(self, store: dict[str, bytes]) -> None:
        self.store = store

    def from_(self, bucket: str):
        return _FakeBucket(self.store)


class _FakeClient:
    def __init__(self, store: dict[str, bytes]) -> None:
        self.storage = _FakeStorage(store)


@pytest.fixture
def fake_storage(monkeypatch):
    """Reemplaza el cliente Supabase por uno en memoria (dict key->bytes)."""
    store: dict[str, bytes] = {}
    monkeypatch.setattr(uploads_storage, "_client", lambda: _FakeClient(store))
    return store


class TestWriteUpload:
    async def test_returns_key_keyed_by_upload_id(self, fake_storage):
        upload_id = uuid.uuid4()
        key = await write_upload(upload_id, b"data")
        assert key == f"{upload_id}.csv"

    async def test_uploads_content_to_bucket(self, fake_storage):
        upload_id = uuid.uuid4()
        content = b"role,content\nuser,hola\nagent,buenas"
        key = await write_upload(upload_id, content)
        assert fake_storage[key] == content

    async def test_handles_empty_content(self, fake_storage):
        key = await write_upload(uuid.uuid4(), b"")
        assert fake_storage[key] == b""

    async def test_overwrites_same_upload_id(self, fake_storage):
        upload_id = uuid.uuid4()
        await write_upload(upload_id, b"first")
        key = await write_upload(upload_id, b"second")
        assert fake_storage[key] == b"second"


class TestRoundtrip:
    async def test_write_then_read_returns_same_bytes(self, fake_storage):
        upload_id = uuid.uuid4()
        content = b"conversation_id,role,content,timestamp\n1,user,hola,2026-01-01"
        key = await write_upload(upload_id, content)
        assert await read_upload(key) == content

    async def test_delete_removes_blob(self, fake_storage):
        upload_id = uuid.uuid4()
        key = await write_upload(upload_id, b"x")
        await delete_upload_blob(key)
        with pytest.raises(KeyError):
            await read_upload(key)


class TestErrorPaths:
    async def test_raises_when_supabase_not_configured(self, monkeypatch):
        """Sin SUPABASE_URL / SERVICE_KEY el cliente falla claro, no silencioso."""
        uploads_storage._client.cache_clear()

        class _NoCreds:
            supabase_url = None
            supabase_service_key = None

        monkeypatch.setattr(uploads_storage, "get_settings", lambda: _NoCreds())
        try:
            with pytest.raises(RuntimeError):
                uploads_storage._client()
        finally:
            uploads_storage._client.cache_clear()
