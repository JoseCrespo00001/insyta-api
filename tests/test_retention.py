"""DB-backed tests for the retention worker (EQUIP-97)."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

_FERNET_KEY = Fernet.generate_key().decode()
os.environ.setdefault("WEBHOOK_SECRET_KEY", _FERNET_KEY)

from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.core.db import async_session_factory  # noqa: E402
from app.workers import retention as retention_module  # noqa: E402


async def _db_available() -> bool:
    try:
        async with async_session_factory() as s:
            await s.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _seed(plan: str = "free") -> dict:
    org_id = uuid.uuid4()
    proj_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    test_ws = Fernet(Fernet.generate_key()).encrypt(b"unused")
    async with async_session_factory() as s:
        async with s.begin():
            await s.execute(
                text(
                    "INSERT INTO organizations(id, public_id, slug, name, plan) "
                    "VALUES (:id, :pid, :slug, 'Org', :plan)"
                ),
                {
                    "id": org_id,
                    "pid": f"org_{org_id.hex[:16]}",
                    "slug": f"org-{org_id.hex[:6]}",
                    "plan": plan,
                },
            )
            await s.execute(
                text(
                    "INSERT INTO projects"
                    "(id, public_id, org_id, slug, name, webhook_secret_encrypted, retention_days) "
                    "VALUES (:id, :pid, :org, 'p', 'P', :ws, 30)"
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


async def _insert_conversation(
    *,
    project_id: uuid.UUID,
    org_id: uuid.UUID,
    agent_id: uuid.UUID,
    created_at: datetime,
    archived_at: datetime | None = None,
) -> uuid.UUID:
    cid = uuid.uuid4()
    async with async_session_factory() as s:
        async with s.begin():
            await s.execute(
                text(
                    "INSERT INTO conversations"
                    "(id, public_id, project_id, org_id, agent_id, external_id, "
                    " platform, status, created_at, archived_at) "
                    "VALUES (:id, :pid, :proj, :org, :agent, :ext, 'wati', "
                    "        'completed', :created, :archived)"
                ),
                {
                    "id": cid,
                    "pid": f"conv_{cid.hex[:16]}",
                    "proj": project_id,
                    "org": org_id,
                    "agent": agent_id,
                    "ext": f"ext-{cid.hex[:8]}",
                    "created": created_at,
                    "archived": archived_at,
                },
            )
    return cid


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.core.db import engine

    await engine.dispose()


@pytest.mark.asyncio
async def test_archive_pass_marks_old_conversations():
    if not await _db_available():
        pytest.skip("PostgreSQL test DB unavailable")
    ids = await _seed(plan="free")
    try:
        old = await _insert_conversation(
            project_id=ids["proj_id"],
            org_id=ids["org_id"],
            agent_id=ids["agent_id"],
            created_at=datetime.now(timezone.utc) - timedelta(days=45),
        )
        recent = await _insert_conversation(
            project_id=ids["proj_id"],
            org_id=ids["org_id"],
            agent_id=ids["agent_id"],
            created_at=datetime.now(timezone.utc) - timedelta(days=5),
        )

        result = await retention_module._archive_pass()
        assert result.get(str(ids["proj_id"])) == 1

        async with async_session_factory() as s:
            row = await s.execute(
                text("SELECT archived_at FROM conversations WHERE id = :id"),
                {"id": old},
            )
            assert row.scalar_one() is not None
            row2 = await s.execute(
                text("SELECT archived_at FROM conversations WHERE id = :id"),
                {"id": recent},
            )
            assert row2.scalar_one() is None
    finally:
        await _cleanup_org(ids["org_id"])


@pytest.mark.asyncio
async def test_archive_pass_idempotent():
    if not await _db_available():
        pytest.skip("PostgreSQL test DB unavailable")
    ids = await _seed(plan="free")
    try:
        await _insert_conversation(
            project_id=ids["proj_id"],
            org_id=ids["org_id"],
            agent_id=ids["agent_id"],
            created_at=datetime.now(timezone.utc) - timedelta(days=45),
        )
        first = await retention_module._archive_pass()
        assert first.get(str(ids["proj_id"])) == 1
        second = await retention_module._archive_pass()
        assert second == {}  # already archived rows are skipped
    finally:
        await _cleanup_org(ids["org_id"])


@pytest.mark.asyncio
async def test_hard_delete_pass_removes_old_archives():
    if not await _db_available():
        pytest.skip("PostgreSQL test DB unavailable")
    ids = await _seed(plan="free")
    try:
        # Archived 31 days ago -> hard delete eligible.
        cid = await _insert_conversation(
            project_id=ids["proj_id"],
            org_id=ids["org_id"],
            agent_id=ids["agent_id"],
            created_at=datetime.now(timezone.utc) - timedelta(days=120),
            archived_at=datetime.now(timezone.utc) - timedelta(days=31),
        )
        # Insert a message so we can verify cascade delete count.
        async with async_session_factory() as s:
            async with s.begin():
                await s.execute(
                    text(
                        "INSERT INTO messages"
                        "(id, public_id, conversation_id, project_id, org_id,"
                        " role, content, timestamp) "
                        "VALUES (:id, :pid, :conv, :proj, :org, 'user', 'x', now())"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "pid": f"msg_{uuid.uuid4().hex[:16]}",
                        "conv": cid,
                        "proj": ids["proj_id"],
                        "org": ids["org_id"],
                    },
                )

        result = await retention_module._hard_delete_pass()
        assert result["conversations"] == 1
        assert result["messages"] == 1

        async with async_session_factory() as s:
            row = await s.execute(
                text("SELECT count(*) FROM conversations WHERE id = :id"),
                {"id": cid},
            )
            assert row.scalar_one() == 0
    finally:
        await _cleanup_org(ids["org_id"])


@pytest.mark.asyncio
async def test_hard_delete_skips_recently_archived():
    if not await _db_available():
        pytest.skip("PostgreSQL test DB unavailable")
    ids = await _seed(plan="free")
    try:
        cid = await _insert_conversation(
            project_id=ids["proj_id"],
            org_id=ids["org_id"],
            agent_id=ids["agent_id"],
            created_at=datetime.now(timezone.utc) - timedelta(days=60),
            archived_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
        result = await retention_module._hard_delete_pass()
        assert result["conversations"] == 0

        async with async_session_factory() as s:
            row = await s.execute(
                text("SELECT count(*) FROM conversations WHERE id = :id"),
                {"id": cid},
            )
            assert row.scalar_one() == 1
    finally:
        await _cleanup_org(ids["org_id"])


@pytest.mark.asyncio
async def test_starter_plan_keeps_conversations_longer():
    if not await _db_available():
        pytest.skip("PostgreSQL test DB unavailable")
    ids = await _seed(plan="starter")
    try:
        # 45 days old: under starter (90d) cap -> not archived.
        await _insert_conversation(
            project_id=ids["proj_id"],
            org_id=ids["org_id"],
            agent_id=ids["agent_id"],
            created_at=datetime.now(timezone.utc) - timedelta(days=45),
        )
        result = await retention_module._archive_pass()
        assert result.get(str(ids["proj_id"]), 0) == 0
    finally:
        await _cleanup_org(ids["org_id"])
