"""Tests para services/uploads_storage.py — persistencia de CSVs subidos.

Módulo de I/O que estaba 0% cubierto (audit 2026-06-24). Prueba el happy-path
(roundtrip a disco) y los error-paths de filesystem.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.services import uploads_storage
from app.services.uploads_storage import write_upload


@pytest.fixture
def tmp_upload_dir(tmp_path: Path, monkeypatch):
    """Redirige UPLOAD_DIR a un tmp aislado por test."""
    target = tmp_path / "uploads"
    monkeypatch.setattr(uploads_storage, "UPLOAD_DIR", target)
    return target


class TestWriteUpload:
    def test_writes_content_and_returns_readable_path(self, tmp_upload_dir):
        upload_id = uuid.uuid4()
        content = b"role,content\nuser,hola\nagent,buenas"
        path_str = write_upload(upload_id, content)

        path = Path(path_str)
        assert path.exists()
        assert path.read_bytes() == content

    def test_path_is_keyed_by_upload_id(self, tmp_upload_dir):
        upload_id = uuid.uuid4()
        path_str = write_upload(upload_id, b"x")
        assert str(upload_id) in path_str
        assert path_str.endswith(".csv")

    def test_creates_dir_if_missing(self, tmp_upload_dir):
        assert not tmp_upload_dir.exists()
        write_upload(uuid.uuid4(), b"data")
        assert tmp_upload_dir.is_dir()

    def test_overwrites_same_upload_id(self, tmp_upload_dir):
        upload_id = uuid.uuid4()
        write_upload(upload_id, b"first")
        path_str = write_upload(upload_id, b"second")
        assert Path(path_str).read_bytes() == b"second"

    def test_handles_empty_content(self, tmp_upload_dir):
        path_str = write_upload(uuid.uuid4(), b"")
        assert Path(path_str).read_bytes() == b""


class TestErrorPaths:
    def test_raises_when_target_dir_path_is_a_file(self, tmp_path: Path, monkeypatch):
        """Si UPLOAD_DIR apunta a un archivo existente, mkdir falla en vez de
        escribir silenciosamente en el lugar equivocado."""
        a_file = tmp_path / "not-a-dir"
        a_file.write_text("soy un archivo")
        monkeypatch.setattr(uploads_storage, "UPLOAD_DIR", a_file)

        with pytest.raises((FileExistsError, NotADirectoryError, OSError)):
            write_upload(uuid.uuid4(), b"data")

    def test_raises_on_unwritable_parent(self, tmp_path: Path, monkeypatch):
        """Directorio padre sin permiso de escritura → OSError, no swallow."""
        ro_parent = tmp_path / "readonly"
        ro_parent.mkdir()
        ro_parent.chmod(0o500)  # r-x: no se puede crear el subdir uploads/
        monkeypatch.setattr(uploads_storage, "UPLOAD_DIR", ro_parent / "uploads")
        try:
            with pytest.raises(OSError):
                write_upload(uuid.uuid4(), b"data")
        finally:
            ro_parent.chmod(0o700)  # restaurar para que pytest limpie el tmp
