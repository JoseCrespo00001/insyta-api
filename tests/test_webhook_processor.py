"""DB-backed tests for app/workers/webhook_processor.py.

Uses the same async_session_factory the worker uses, so engine + handler share
one event loop and asyncpg doesn't drop connections at teardown.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

_FERNET_KEY = Fernet.generate_key().decode()
os.environ.setdefault("WEBHOOK_SECRET_KEY", _FERNET_KEY)

from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.core.db import async_session_factory  # noqa: E402
from app.models import WebhookEvent  # noqa: E402
from app.services import celery_app as celery_app_module  # noqa: E402

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/insyta",
)


async def _db_available() -> bool:
    try:
        async with async_session_factory() as s:
            await s.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _seed() -> dict:
    """Create org/project/agent rows. Returns ids dict."""
    org_id = uuid.uuid4()
    proj_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    test_ws = Fernet(Fernet.generate_key()).encrypt(b"unused")

    async with async_session_factory() as s:
        async with s.begin():
            await s.execute(
                text(
                    "INSERT INTO organizations(id, public_id, slug, name) "
                    "VALUES (:id, :pid, :slug, 'Org')"
                ),
                {
                    "id": org_id,
                    "pid": f"org_{org_id.hex[:16]}",
                    "slug": f"org-{org_id.hex[:6]}",
                },
            )
            await s.execute(
                text(
                    "INSERT INTO projects"
                    "(id, public_id, org_id, slug, name, webhook_secret_encrypted) "
                    "VALUES (:id, :pid, :org, 'p', 'P', :ws)"
                ),
                {
                    "id": proj_id,
                    "pid": f"proj_{proj_id.hex[:16]}",
                    "org": org_id,
                    "ws": test_ws,
                },
            )
            await s.execute(
                text(
                    "INSERT INTO agents"
                    "(id, public_id, project_id, org_id, slug, name, platform) "
                    "VALUES (:id, :pid, :proj, :org, 'a', 'A', 'wati')"
                ),
                {
                    "id": agent_id,
                    "pid": f"agt_{agent_id.hex[:16]}",
                    "proj": proj_id,
                    "org": org_id,
                },
            )
    return {"org_id": org_id, "proj_id": proj_id, "agent_id": agent_id}


async def _cleanup_org(org_id: uuid.UUID) -> None:
    async with async_session_factory() as s:
        async with s.begin():
            await s.execute(
                text("DELETE FROM organizations WHERE id = :id"), {"id": org_id}
            )


async def _insert_event(
    *,
    project_id: uuid.UUID,
    org_id: uuid.UUID,
    payload: dict,
    platform: str = "wati",
) -> uuid.UUID:
    eid = uuid.uuid4()
    async with async_session_factory() as s:
        async with s.begin():
            ev = WebhookEvent(
                id=eid,
                project_id=project_id,
                org_id=org_id,
                platform=platform,
                payload=payload,
                signature_verified=True,
                idempotency_key=uuid.uuid4().hex,
                status="pending",
                received_at=datetime.now(timezone.utc),
            )
            s.add(ev)
    return eid


@pytest.fixture(autouse=True)
def _stub_celery(monkeypatch):
    calls: list[tuple] = []

    def fake_send_task(name: str, args=None, kwargs=None, **opts):
        calls.append((name, args, kwargs))

    monkeypatch.setattr(celery_app_module.celery_app, "send_task", fake_send_task)
    yield calls


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    """Each pytest-asyncio function gets a fresh loop; the global engine's
    asyncpg connections from a prior loop are useless. Dispose so the next
    test acquires connections on its own loop.
    """
    yield
    from app.core.db import engine

    await engine.dispose()


@pytest.mark.asyncio
async def test_process_creates_conversation_and_message(_stub_celery):
    if not await _db_available():
        pytest.skip("PostgreSQL test DB unavailable")
    ids = await _seed()
    try:
        eid = await _insert_event(
            project_id=ids["proj_id"],
            org_id=ids["org_id"],
            payload={
                "chat_id": "ext-001",
                "direction": "inbound",
                "text": "hola",
                "timestamp": "2026-04-29T10:00:00Z",
            },
        )
        from app.workers.webhook_processor import _process

        result = await _process(eid)
        assert result["was_new_conversation"] is True
        assert result["messages_persisted"] == 1
        assert result["terminal"] is False

        async with async_session_factory() as s:
            row = await s.execute(
                text("SELECT status FROM webhook_events WHERE id = :id"),
                {"id": eid},
            )
            assert row.scalar_one() == "processed"
            count = await s.execute(
                text("SELECT count(*) FROM messages " "WHERE conversation_id = :cid"),
                {"cid": uuid.UUID(result["conversation_id"])},
            )
            assert count.scalar_one() == 1
    finally:
        await _cleanup_org(ids["org_id"])


@pytest.mark.asyncio
async def test_process_terminal_payload_closes_and_enqueues_eval(_stub_celery):
    if not await _db_available():
        pytest.skip("PostgreSQL test DB unavailable")
    ids = await _seed()
    try:
        eid = await _insert_event(
            project_id=ids["proj_id"],
            org_id=ids["org_id"],
            payload={
                "chat_id": "ext-end",
                "direction": "inbound",
                "text": "bye",
                "status": "ended",
            },
        )
        from app.workers.webhook_processor import _process

        result = await _process(eid)
        assert result["terminal"] is True

        async with async_session_factory() as s:
            row = await s.execute(
                text("SELECT status FROM conversations WHERE id = :id"),
                {"id": uuid.UUID(result["conversation_id"])},
            )
            assert row.scalar_one() == "completed"

        eval_calls = [
            c
            for c in _stub_celery
            if c[0] == "app.workers.evaluator.evaluate_conversation"
        ]
        assert len(eval_calls) == 1
    finally:
        await _cleanup_org(ids["org_id"])


@pytest.mark.asyncio
async def test_process_no_agent_fails_event(_stub_celery):
    if not await _db_available():
        pytest.skip("PostgreSQL test DB unavailable")
    org_id = uuid.uuid4()
    proj_id = uuid.uuid4()
    test_ws = Fernet(Fernet.generate_key()).encrypt(b"unused")
    try:
        async with async_session_factory() as s:
            async with s.begin():
                await s.execute(
                    text(
                        "INSERT INTO organizations(id, public_id, slug, name) "
                        "VALUES (:id, :pid, :slug, 'Org')"
                    ),
                    {
                        "id": org_id,
                        "pid": f"org_{org_id.hex[:16]}",
                        "slug": f"org-{org_id.hex[:6]}",
                    },
                )
                await s.execute(
                    text(
                        "INSERT INTO projects"
                        "(id, public_id, org_id, slug, name, webhook_secret_encrypted) "
                        "VALUES (:id, :pid, :org, 'p', 'P', :ws)"
                    ),
                    {
                        "id": proj_id,
                        "pid": f"proj_{proj_id.hex[:16]}",
                        "org": org_id,
                        "ws": test_ws,
                    },
                )

        eid = await _insert_event(
            project_id=proj_id, org_id=org_id, payload={"text": "x"}
        )
        from app.workers.webhook_processor import _process

        result = await _process(eid)
        assert result.get("failed") is True
        assert result.get("reason") == "no_agent"

        async with async_session_factory() as s:
            row = await s.execute(
                text("SELECT status, error_message FROM webhook_events WHERE id = :id"),
                {"id": eid},
            )
            r = row.one()
            assert r.status == "failed"
            assert "no agent" in r.error_message
    finally:
        await _cleanup_org(org_id)


@pytest.mark.asyncio
async def test_process_idempotent_replay(_stub_celery):
    if not await _db_available():
        pytest.skip("PostgreSQL test DB unavailable")
    ids = await _seed()
    try:
        payload = {"chat_id": "ext-replay", "text": "first"}
        eid_1 = await _insert_event(
            project_id=ids["proj_id"], org_id=ids["org_id"], payload=payload
        )
        eid_2 = await _insert_event(
            project_id=ids["proj_id"], org_id=ids["org_id"], payload=payload
        )

        from app.workers.webhook_processor import _process

        r1 = await _process(eid_1)
        r2 = await _process(eid_2)

        assert r1["was_new_conversation"] is True
        assert r2["was_new_conversation"] is False
        assert r1["conversation_id"] == r2["conversation_id"]
    finally:
        await _cleanup_org(ids["org_id"])
