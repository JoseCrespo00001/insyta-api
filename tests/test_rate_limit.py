"""Rate limit tests use an in-memory limiter so they don't touch Redis."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import AsyncIterator

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from slowapi import Limiter
from slowapi.util import get_remote_address

# Set the Fernet key BEFORE importing routers so the helper picks it up.
_FERNET_KEY = Fernet.generate_key()
os.environ.setdefault("WEBHOOK_SECRET_KEY", _FERNET_KEY.decode())

from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.core.db import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services import rate_limit as rate_limit_module  # noqa: E402
from app.services.webhook_secret import (  # noqa: E402
    _fernet,
    encrypt_webhook_secret,
)

_fernet.cache_clear()

PROJECT_ID = "proj_rl_test"
SECRET = "rate-limit-test-secret-32-chars--"
SECRET_ENCRYPTED = encrypt_webhook_secret(SECRET)


class _StubRow:
    def __init__(self) -> None:
        import uuid as _uuid

        self.id = _uuid.uuid4()
        self.org_id = _uuid.uuid4()
        self.webhook_secret_encrypted = SECRET_ENCRYPTED


class _StubResult:
    def __init__(self, *, row=None, scalar=None) -> None:
        self._row = row
        self._scalar = scalar

    def one_or_none(self):
        return self._row

    def scalar_one_or_none(self):
        return self._scalar


class _StubSession:
    async def execute(self, stmt):  # type: ignore[no-untyped-def]
        stmt_text = str(stmt)
        if "FROM projects" in stmt_text:
            return _StubResult(row=_StubRow())
        if "INTO webhook_events" in stmt_text:
            import uuid as _uuid

            return _StubResult(scalar=_uuid.uuid4())
        return _StubResult(row=None, scalar=None)

    async def commit(self):
        pass


async def _override_get_db() -> AsyncIterator[_StubSession]:
    yield _StubSession()


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def small_limit_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Replaces the production limiter with an in-memory 5/minute limiter.

    Patching a smaller limit keeps the test fast (no need for 1000 reqs). The
    webhooks module is reloaded so the decorator captures the small limit;
    teardown reloads it again with the production constants so other test
    files (test_webhooks.py etc.) see the original 1000/minute decorator.
    """
    test_limiter = Limiter(
        key_func=get_remote_address,
        storage_uri="memory://",
        strategy="fixed-window",
        default_limits=[],
    )
    monkeypatch.setattr(rate_limit_module, "limiter", test_limiter)
    monkeypatch.setattr(rate_limit_module, "WEBHOOK_RATE_LIMIT", "5/minute")

    from importlib import reload

    from app.routers import webhooks as webhooks_module

    reload(webhooks_module)

    new_app = app
    original_routes = list(new_app.router.routes)
    new_app.state.limiter = test_limiter
    new_app.router.routes = [
        r
        for r in new_app.router.routes
        if not (getattr(r, "path", "").startswith("/webhooks/"))
    ]
    new_app.include_router(webhooks_module.router)
    new_app.dependency_overrides[get_db] = _override_get_db

    with TestClient(new_app) as c:
        yield c

    new_app.dependency_overrides.clear()
    monkeypatch.undo()
    # Reload again so the decorator is bound to the production limit and
    # production limiter (1000/minute, conftest's memory limiter).
    reload(webhooks_module)
    new_app.router.routes = [
        r
        for r in original_routes
        if not (getattr(r, "path", "").startswith("/webhooks/"))
    ]
    new_app.include_router(webhooks_module.router)


class TestRateLimit:
    def test_within_limit_returns_200(self, small_limit_client: TestClient):
        body = json.dumps({"event": "ok"}).encode()
        sig = _sign(body)
        for _ in range(5):
            r = small_limit_client.post(
                f"/webhooks/wati/{PROJECT_ID}",
                content=body,
                headers={
                    "x-wati-signature": sig,
                    "content-type": "application/json",
                },
            )
            assert r.status_code == 200

    def test_exceeding_limit_returns_429(self, small_limit_client: TestClient):
        body = json.dumps({"event": "ok"}).encode()
        sig = _sign(body)
        for _ in range(5):
            small_limit_client.post(
                f"/webhooks/wati/{PROJECT_ID}",
                content=body,
                headers={
                    "x-wati-signature": sig,
                    "content-type": "application/json",
                },
            )
        r = small_limit_client.post(
            f"/webhooks/wati/{PROJECT_ID}",
            content=body,
            headers={
                "x-wati-signature": sig,
                "content-type": "application/json",
            },
        )
        assert r.status_code == 429

    def test_rate_limit_keyed_per_endpoint(self, small_limit_client: TestClient):
        body = json.dumps({"event": "ok"}).encode()
        sig = _sign(body)
        for _ in range(5):
            small_limit_client.post(
                f"/webhooks/wati/{PROJECT_ID}",
                content=body,
                headers={
                    "x-wati-signature": sig,
                    "content-type": "application/json",
                },
            )
        # respondio endpoint has its own bucket per route.
        r = small_limit_client.post(
            f"/webhooks/respondio/{PROJECT_ID}",
            content=body,
            headers={
                "x-respondio-signature": sig,
                "content-type": "application/json",
            },
        )
        assert r.status_code == 200
