"""Tests for /api/v1/feed/stream and the feed_bus helper.

The bus tests need a working Redis (skip if absent). The endpoint test stubs
the subscribe_tenant_feed iterator so it does not require Redis.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport
from jose import jwt

_FERNET_KEY = Fernet.generate_key().decode()
os.environ.setdefault("WEBHOOK_SECRET_KEY", _FERNET_KEY)

from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.main import app  # noqa: E402
from app.routers import feed as feed_module  # noqa: E402
from app.services import feed_bus  # noqa: E402


@pytest.fixture(autouse=True)
def _settings_for_jwt(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "x" * 32)
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("WEBHOOK_SECRET_KEY", _FERNET_KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_token(org_id: uuid.UUID) -> str:
    return jwt.encode(
        {
            "sub": "test-user",
            "email": "test@insyta.io",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
            "org_id": str(org_id),
            "allowed_project_ids": [],
        },
        "x" * 32,
        algorithm="HS256",
    )


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_unauthenticated_returns_401():
    async with _client() as c:
        r = await c.get("/api/v1/feed/stream")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_authenticated_subscribes_and_streams_published_event(monkeypatch):
    org_id = uuid.uuid4()

    captured_org: list[uuid.UUID] = []

    async def fake_subscribe(target_org_id: uuid.UUID) -> AsyncIterator[str]:
        captured_org.append(target_org_id)
        # Yield one event then close so EventSourceResponse finishes.
        yield json.dumps({"conv_id": "abc", "score": 80})

    monkeypatch.setattr(feed_module, "subscribe_tenant_feed", fake_subscribe)

    token = _make_token(org_id)
    async with _client() as c:
        async with c.stream(
            "GET",
            "/api/v1/feed/stream",
            headers={"Authorization": f"Bearer {token}"},
        ) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            chunks = []
            async for line in r.aiter_lines():
                chunks.append(line)
                if any('"conv_id": "abc"' in c for c in chunks):
                    break

    assert captured_org == [org_id]
    rendered = "\n".join(chunks)
    assert "event: evaluation" in rendered
    assert '"conv_id": "abc"' in rendered


def _redis_available() -> bool:
    import redis as redis_sync

    try:
        c = redis_sync.Redis.from_url(get_settings().redis_url)
        c.ping()
        return True
    except Exception:
        return False


@pytest.mark.asyncio
async def test_publish_then_subscribe_round_trip():
    if not _redis_available():
        pytest.skip("Redis not reachable")
    org_id = uuid.uuid4()

    received: list[str] = []
    finished = asyncio.Event()

    async def consume():
        async for raw in feed_bus.subscribe_tenant_feed(org_id):
            received.append(raw)
            finished.set()
            break

    consumer = asyncio.create_task(consume())
    # Give the subscription a moment to register.
    await asyncio.sleep(0.1)
    feed_bus.publish_eval_event(
        org_id=org_id,
        conv_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        score=75,
        topic="t",
        evaluated_at=None,
    )
    try:
        await asyncio.wait_for(finished.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        consumer.cancel()
        raise

    assert len(received) == 1
    assert json.loads(received[0])["score"] == 75
    consumer.cancel()
    try:
        await consumer
    except asyncio.CancelledError:
        pass
