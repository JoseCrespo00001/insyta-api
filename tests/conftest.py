"""Test fixtures.

`postgres_engine` connects to the docker-compose Postgres on port 5433. The
session fixture truncates touched tables between tests to keep RLS assertions
isolated. If the database is not available we skip RLS tests rather than fail.

The slowapi limiter is replaced with an in-memory storage at import time so the
default Redis-backed limiter never tries to connect during tests.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.services import rate_limit as _rate_limit_module

_rate_limit_module.limiter = Limiter(
    key_func=get_remote_address,
    storage_uri="memory://",
    strategy="fixed-window",
    default_limits=[],
)

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/insyta",
)


async def _db_available(url: str) -> bool:
    eng = create_async_engine(url, pool_pre_ping=True)
    try:
        async with eng.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    if not await _db_available(TEST_DATABASE_URL):
        pytest.skip("PostgreSQL test DB unavailable on 5433 — start docker-compose")
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(postgres_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def seeded_two_orgs(postgres_engine: AsyncEngine) -> AsyncIterator[dict]:
    """Insert 2 orgs, 1 project + 1 agent + 1 conversation each, as a superuser
    bypassing RLS (we authenticate as the postgres role).

    Returns the ids needed by tests.
    """
    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    proj_a = uuid.uuid4()
    proj_b = uuid.uuid4()
    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()
    conv_a = uuid.uuid4()
    conv_b = uuid.uuid4()

    async with factory() as s:
        async with s.begin():
            # Bypass RLS for setup — postgres user is superuser, RLS does not
            # apply to BYPASSRLS roles, but FORCE was set, so we must be the
            # table owner. The postgres docker image creates the postgres role
            # as the owner of tables, so this works.
            for oid, slug, name in [
                (org_a, f"org-a-{org_a.hex[:6]}", "Org A"),
                (org_b, f"org-b-{org_b.hex[:6]}", "Org B"),
            ]:
                await s.execute(
                    text(
                        "INSERT INTO organizations(id, public_id, slug, name) "
                        "VALUES (:id, :pid, :slug, :name)"
                    ),
                    {
                        "id": oid,
                        "pid": f"org_{oid.hex[:16]}",
                        "slug": slug,
                        "name": name,
                    },
                )
            for pid, oid, slug in [
                (proj_a, org_a, f"proj-a-{proj_a.hex[:6]}"),
                (proj_b, org_b, f"proj-b-{proj_b.hex[:6]}"),
            ]:
                await s.execute(
                    text(
                        "INSERT INTO projects"
                        "(id, public_id, org_id, slug, name, webhook_secret) "
                        "VALUES (:id, :pid, :oid, :slug, :name, :ws)"
                    ),
                    {
                        "id": pid,
                        "pid": f"proj_{pid.hex[:16]}",
                        "oid": oid,
                        "slug": slug,
                        "name": "Project",
                        "ws": "test-secret",
                    },
                )
            for aid, pid, oid in [
                (agent_a, proj_a, org_a),
                (agent_b, proj_b, org_b),
            ]:
                await s.execute(
                    text(
                        "INSERT INTO agents"
                        "(id, public_id, project_id, org_id, slug, name, platform) "
                        "VALUES (:id, :pid, :proj, :org, :slug, :name, :plat)"
                    ),
                    {
                        "id": aid,
                        "pid": f"agt_{aid.hex[:16]}",
                        "proj": pid,
                        "org": oid,
                        "slug": "agent",
                        "name": "Agent",
                        "plat": "custom_sdk",
                    },
                )
            for cid, pid, oid, aid, ext in [
                (conv_a, proj_a, org_a, agent_a, "ext-a"),
                (conv_b, proj_b, org_b, agent_b, "ext-b"),
            ]:
                await s.execute(
                    text(
                        "INSERT INTO conversations"
                        "(id, public_id, project_id, org_id, agent_id,"
                        " external_id, platform) "
                        "VALUES (:id, :pid, :proj, :org, :agent, :ext, :plat)"
                    ),
                    {
                        "id": cid,
                        "pid": f"conv_{cid.hex[:16]}",
                        "proj": pid,
                        "org": oid,
                        "agent": aid,
                        "ext": ext,
                        "plat": "custom_sdk",
                    },
                )

    yield {
        "org_a": org_a,
        "org_b": org_b,
        "proj_a": proj_a,
        "proj_b": proj_b,
        "agent_a": agent_a,
        "agent_b": agent_b,
        "conv_a": conv_a,
        "conv_b": conv_b,
    }

    # Cleanup
    async with factory() as s:
        async with s.begin():
            await s.execute(
                text("DELETE FROM organizations WHERE id IN (:a, :b)"),
                {"a": org_a, "b": org_b},
            )
