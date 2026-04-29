import hashlib
import hmac
import json
import os
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
from app.services.webhook_secret import (  # noqa: E402
    _fernet,
    encrypt_webhook_secret,
)

# Reset the Fernet lru_cache so it picks up the env-configured key.
_fernet.cache_clear()

PROJECT_A_ID = "proj_aaaa"
PROJECT_A_SECRET = "secret-aaaa-32-chars-long-padding-x"
PROJECT_B_ID = "proj_bbbb"
PROJECT_B_SECRET = "secret-bbbb-32-chars-long-padding-x"

PROJECT_SECRETS = {
    PROJECT_A_ID: PROJECT_A_SECRET,
    PROJECT_B_ID: PROJECT_B_SECRET,
}
PROJECT_ENCRYPTED = {
    pid: encrypt_webhook_secret(secret) for pid, secret in PROJECT_SECRETS.items()
}


class _StubScalarResult:
    def __init__(self, value: bytes | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> bytes | None:
        return self._value


class _StubSession:
    """Returns the encrypted webhook_secret for known project public_ids."""

    async def execute(self, stmt):  # type: ignore[no-untyped-def]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        for public_id, encrypted in PROJECT_ENCRYPTED.items():
            if f"'{public_id}'" in compiled:
                return _StubScalarResult(encrypted)
        return _StubScalarResult(None)


async def _override_get_db() -> AsyncIterator[_StubSession]:
    yield _StubSession()


@pytest.fixture
def client() -> TestClient:
    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class TestWatiWebhookHmac:
    def test_valid_signature_returns_200(self, client: TestClient):
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

    def test_invalid_signature_returns_401(self, client: TestClient):
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

    def test_missing_header_returns_401(self, client: TestClient):
        r = client.post(
            f"/webhooks/wati/{PROJECT_A_ID}",
            json={"event": "message"},
        )
        assert r.status_code == 401
        assert "Missing signature" in r.json()["detail"]

    def test_signature_from_other_project_returns_401(self, client: TestClient):
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


class TestRespondioWebhookHmac:
    def test_valid_signature_returns_200(self, client: TestClient):
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
