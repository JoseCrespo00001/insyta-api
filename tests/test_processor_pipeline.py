"""End-to-end test of the CSV → conversations+messages pipeline (no eval).

Exercises `app.workers.processor._run` against the real thesis golden-set CSV.
Evaluation is NOT enqueued here — it runs at audit time.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.workers import processor

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/insyta",
)
CSV_PATH = Path(__file__).parent / "fixtures" / "samples_demo_tesis.csv"


async def _db_available() -> bool:
    eng = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with eng.connect() as c:
            await c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_run_creates_conversations_and_messages(monkeypatch):
    if not await _db_available() or not CSV_PATH.exists():
        pytest.skip("DB unavailable or CSV fixture missing")

    # processor uses the module-level async_session_factory bound to DATABASE_URL.
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

    eng = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(eng, expire_on_commit=False)

    org_id = uuid.uuid4()
    proj_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    upload_id = uuid.uuid4()
    try:
        async with factory() as s, s.begin():
            await s.execute(
                text(
                    "INSERT INTO organizations(id, public_id, slug, name) "
                    "VALUES (:id, :pid, :slug, 'Org')"
                ),
                {
                    "id": org_id,
                    "pid": f"org_{org_id.hex[:16]}",
                    "slug": f"o-{org_id.hex[:6]}",
                },
            )
            await s.execute(
                text(
                    "INSERT INTO projects(id, public_id, org_id, slug, name) "
                    "VALUES (:id, :pid, :org, :slug, 'P')"
                ),
                {
                    "id": proj_id,
                    "pid": f"proj_{proj_id.hex[:16]}",
                    "org": org_id,
                    "slug": f"p-{proj_id.hex[:6]}",
                },
            )
            await s.execute(
                text(
                    "INSERT INTO agents(id, public_id, project_id, org_id, slug, name, platform) "
                    "VALUES (:id, :pid, :proj, :org, 'default', 'A', 'custom_sdk')"
                ),
                {
                    "id": agent_id,
                    "pid": f"agt_{agent_id.hex[:16]}",
                    "proj": proj_id,
                    "org": org_id,
                },
            )
            await s.execute(
                text(
                    "INSERT INTO uploads(id, public_id, org_id, project_id, agent_id, "
                    "filename, raw_content, size_bytes, status) "
                    "VALUES (:id, :pid, :org, :proj, :agent, 'demo.csv', :raw, :sz, 'pending')"
                ),
                {
                    "id": upload_id,
                    "pid": f"upl_{upload_id.hex[:16]}",
                    "org": org_id,
                    "proj": proj_id,
                    "agent": agent_id,
                    "raw": CSV_PATH.read_bytes(),
                    "sz": CSV_PATH.stat().st_size,
                },
            )

        summary = await processor._run(upload_id, org_id)
        assert summary["new_conversations"] > 0
        assert summary["messages_persisted"] > summary["new_conversations"]

        async with factory() as s:
            conv_count = (
                await s.execute(
                    text("SELECT count(*) FROM conversations WHERE upload_id = :u"),
                    {"u": upload_id},
                )
            ).scalar_one()
            assert conv_count == summary["new_conversations"]

            # preview + message_count + status set
            row = (
                await s.execute(
                    text(
                        "SELECT preview, message_count, status FROM conversations "
                        "WHERE upload_id = :u LIMIT 1"
                    ),
                    {"u": upload_id},
                )
            ).one()
            assert row.preview
            assert row.message_count > 0
            assert row.status == "completed"

            # messages have stable seq starting at 0
            seqs = (
                (
                    await s.execute(
                        text(
                            "SELECT seq FROM messages WHERE project_id = :p ORDER BY seq LIMIT 3"
                        ),
                        {"p": proj_id},
                    )
                )
                .scalars()
                .all()
            )
            assert seqs[0] == 0

            # upload marked completed
            ust = (
                await s.execute(
                    text("SELECT status, rows_total FROM uploads WHERE id = :u"),
                    {"u": upload_id},
                )
            ).one()
            assert ust.status == "completed"
            assert ust.rows_total > 0
    finally:
        async with factory() as s, s.begin():
            await s.execute(
                text("DELETE FROM organizations WHERE id = :id"), {"id": org_id}
            )
        await eng.dispose()
