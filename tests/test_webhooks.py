"""HMAC + persistence tests for /webhooks/{wati,respondio}.

Uses a stub session so the tests do not require Postgres. The stub recognises
two statement shapes:

- The SELECT against `Project` that resolves the encrypted secret.
- The INSERT … ON CONFLICT … RETURNING against `WebhookEvent`.

Celery dispatch is monkeypatched so we don't talk to a broker.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

# Ensure WEBHOOK_SECRET_KEY is set before any module reads settings via Fernet.
_TEST_FERNET_KEY = Fernet.generate_key()
os.environ.setdefault("WEBHOOK_SECRET_KEY", _TEST_FERNET_KEY.decode())

from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.core.db import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services import celery_app as celery_app_module  # noqa: E402
from app.services.webhook_secret import (  # noqa: E402
    _fernet,
    encrypt_webhook_secret,
)

_fernet.cache_clear()

PROJECT_A_ID = "proj_aaaa"
PROJECT_A_SECRET = "secret-aaaa-32-chars-long-padding-x"
PROJECT_B_ID = "proj_bbbb"
PROJECT_B_SECRET = "secret-bbbb-32-chars-long-padding-x"

PROJECT_DATA = {
    PROJECT_A_ID: (
        uuid.uuid4(),
        uuid.uuid4(),
        encrypt_webhook_secret(PROJECT_A_SECRET),
    ),
    PROJECT_B_ID: (
        uuid.uuid4(),
        uuid.uuid4(),
        encrypt_webhook_secret(PROJECT_B_SECRET),
    ),
}


class _StubRow:
    def __init__(self, id: uuid.UUID, org_id: uuid.UUID, encrypted: bytes) -> None:
        self.id = id
        self.org_id = org_id
        self.webhook_secret_encrypted = encrypted


class _StubResult:
    def __init__(
        self,
        *,
        row: _StubRow | None = None,
        scalar: object | None = None,
    ) -> None:
        self._row = row
        self._scalar = scalar

    def one_or_none(self):
        return self._row

    def scalar_one_or_none(self):
        return self._scalar


class _StubSession:
    """Routes statements by inspecting their compiled SQL.

    - SELECT projects.id ... -> returns a Row with id/org_id/encrypted.
    - INSERT INTO webhook_events ... RETURNING -> returns a uuid (was_new) or None.
    """

    def __init__(self) -> None:
        self.seen_idem: set[tuple[str, str]] = set()
        self.committed = False

    async def execute(self, stmt):  # type: ignore[no-untyped-def]
        stmt_text = str(stmt)

        if "FROM projects" in stmt_text:
            compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            for public_id, (pid, oid, encrypted) in PROJECT_DATA.items():
                if f"'{public_id}'" in compiled:
                    return _StubResult(row=_StubRow(pid, oid, encrypted))
            return _StubResult(row=None)

        if "INTO webhook_events" in stmt_text:
            params = getattr(stmt, "compile", lambda **_: None)()
            project_id = None
            idem = None
            try:
                values = params.params if params is not None else {}
                project_id = str(values.get("project_id"))
                idem = str(values.get("idempotency_key"))
            except Exception:
                pass
            if project_id and idem:
                key = (project_id, idem)
                if key in self.seen_idem:
                    return _StubResult(scalar=None)
                self.seen_idem.add(key)
            return _StubResult(scalar=uuid.uuid4())

        return _StubResult(row=None, scalar=None)

    async def commit(self):
        self.committed = True


_STUB_SESSION = _StubSession()


async def _override_get_db() -> AsyncIterator[_StubSession]:
    yield _STUB_SESSION


@pytest.fixture
def enqueued(monkeypatch):
    calls: list[tuple] = []

    def fake_send_task(name: str, args=None, kwargs=None, **opts):
        calls.append((name, args, kwargs))

    monkeypatch.setattr(celery_app_module.celery_app, "send_task", fake_send_task)
    yield calls


@pytest.fixture
def client(enqueued) -> TestClient:
    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class TestWatiWebhookHmac:
    def test_valid_signature_returns_200(self, client: TestClient, enqueued):
        body = json.dumps({"event": "message", "id": "1"}).encode()
        sig = _sign(PROJECT_A_SECRET, body)
        r = client.post(
            f"/webhooks/wati/{PROJECT_A_ID}",
            content=body,
            headers={
                "x-wati-signature": sig,
                "content-type": "application/json",
            },
        )
        assert r.status_code == 200
        assert r.json() == {"received": True, "platform": "wati"}
        assert len(enqueued) == 1
        assert enqueued[0][0] == "app.workers.webhook_processor.process_webhook_event"

    def test_invalid_signature_returns_401(self, client: TestClient, enqueued):
        body = json.dumps({"event": "message"}).encode()
        r = client.post(
            f"/webhooks/wati/{PROJECT_A_ID}",
            content=body,
            headers={
                "x-wati-signature": "sha256=" + "0" * 64,
                "content-type": "application/json",
            },
        )
        assert r.status_code == 401
        assert enqueued == []

    def test_missing_header_returns_401(self, client: TestClient):
        r = client.post(
            f"/webhooks/wati/{PROJECT_A_ID}",
            json={"event": "message"},
        )
        assert r.status_code == 401
        assert "Missing signature" in r.json()["detail"]

    def test_signature_from_other_project_returns_401(
        self, client: TestClient, enqueued
    ):
        body = json.dumps({"event": "message"}).encode()
        sig = _sign(PROJECT_B_SECRET, body)
        r = client.post(
            f"/webhooks/wati/{PROJECT_A_ID}",
            content=body,
            headers={
                "x-wati-signature": sig,
                "content-type": "application/json",
            },
        )
        assert r.status_code == 401
        assert enqueued == []

    def test_unknown_project_returns_401(self, client: TestClient):
        body = json.dumps({"event": "message"}).encode()
        sig = _sign("any-secret", body)
        r = client.post(
            "/webhooks/wati/proj_does_not_exist",
            content=body,
            headers={
                "x-wati-signature": sig,
                "content-type": "application/json",
            },
        )
        assert r.status_code == 401

    def test_signature_without_prefix_returns_401(self, client: TestClient):
        body = json.dumps({"event": "message"}).encode()
        raw_hex = hmac.new(PROJECT_A_SECRET.encode(), body, hashlib.sha256).hexdigest()
        r = client.post(
            f"/webhooks/wati/{PROJECT_A_ID}",
            content=body,
            headers={
                "x-wati-signature": raw_hex,
                "content-type": "application/json",
            },
        )
        assert r.status_code == 401

    def test_tampered_body_returns_401(self, client: TestClient):
        body = json.dumps({"event": "message"}).encode()
        sig = _sign(PROJECT_A_SECRET, body)
        tampered = json.dumps({"event": "message", "extra": "x"}).encode()
        r = client.post(
            f"/webhooks/wati/{PROJECT_A_ID}",
            content=tampered,
            headers={
                "x-wati-signature": sig,
                "content-type": "application/json",
            },
        )
        assert r.status_code == 401

    def test_non_json_body_returns_400(self, client: TestClient):
        body = b"not json at all"
        sig = _sign(PROJECT_A_SECRET, body)
        r = client.post(
            f"/webhooks/wati/{PROJECT_A_ID}",
            content=body,
            headers={
                "x-wati-signature": sig,
                "content-type": "application/json",
            },
        )
        assert r.status_code == 400


class TestRespondioWebhookHmac:
    def test_valid_signature_returns_200(self, client: TestClient, enqueued):
        body = json.dumps({"event": "incoming"}).encode()
        sig = _sign(PROJECT_A_SECRET, body)
        r = client.post(
            f"/webhooks/respondio/{PROJECT_A_ID}",
            content=body,
            headers={
                "x-respondio-signature": sig,
                "content-type": "application/json",
            },
        )
        assert r.status_code == 200
        assert r.json() == {"received": True, "platform": "respondio"}
        assert len(enqueued) == 1

    def test_missing_header_returns_401(self, client: TestClient):
        r = client.post(
            f"/webhooks/respondio/{PROJECT_A_ID}",
            json={"event": "incoming"},
        )
        assert r.status_code == 401

    def test_invalid_signature_returns_401(self, client: TestClient):
        body = json.dumps({"event": "incoming"}).encode()
        r = client.post(
            f"/webhooks/respondio/{PROJECT_A_ID}",
            content=body,
            headers={
                "x-respondio-signature": "sha256=" + "f" * 64,
                "content-type": "application/json",
            },
        )
        assert r.status_code == 401
