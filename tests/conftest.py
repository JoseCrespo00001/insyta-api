"""Test fixtures.

`postgres_engine` connects to the docker-compose Postgres on port 5433. The
session fixture truncates touched tables between tests to keep RLS assertions
isolated. If the database is not available we skip RLS tests rather than fail.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_module_engine():
    """Dispose the module-level async engine after each test so its pooled
    asyncpg connections (bound to this test's event loop) don't leak into the
    next test's loop ('attached to a different loop' / 'Event loop is closed')."""
    yield
    try:
        from app.core import db

        await db.engine.dispose()
    except Exception:
        pass


TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    # DB DEDICADA de tests — NUNCA la DB de la app (`insyta`). Los tests dropean
    # y re-crean el schema; si apuntaran a `insyta` borrarían los datos reales.
    "postgresql+asyncpg://postgres:postgres@localhost:5433/insyta_test",
)

# ---------------------------------------------------------------------------
# GUARDA DURA ANTI-PROD (aprendido a los golpes: correr pytest/alembic contra
# la DB de prod borró proyectos/conversaciones reales — cascade desde projects).
#
# Los tests SOLO pueden tocar una DB LOCAL. Chequeamos DOS cosas:
#   1. El engine de la app (get_settings().database_url) — resuelve el `.env`,
#      cosa que os.getenv NO ve. Los worker-tests usan este engine; si apunta a
#      Supabase/prod, un test podría escribir/borrar en producción.
#   2. TEST_DATABASE_URL — la DB dedicada que los fixtures truncan/reseteean.
# Si CUALQUIERA no es local, abortamos TODA la sesión de tests (import-time).
# ---------------------------------------------------------------------------
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres", "db", ""}


def _db_host(url: str) -> str:
    """Extrae el host de una URL sqlalchemy (postgresql+asyncpg://u:p@HOST:port/db)."""
    try:
        after_at = url.split("@", 1)[1] if "@" in url else url.split("://", 1)[1]
        return after_at.split("/", 1)[0].rsplit(":", 1)[0].strip("[]").lower()
    except (IndexError, AttributeError):
        return url


def _assert_local(url: str, label: str) -> None:
    host = _db_host(url)
    if host not in _LOCAL_HOSTS:
        raise RuntimeError(
            f"{label} apunta a una DB REMOTA/PROD (host={host!r}). Los tests NO "
            f"pueden correr contra prod — borrarían datos reales. Usá una DB "
            f"local (localhost:5433). Seteá {label} explícitamente antes de pytest."
        )


# 1. El engine real de la app (incluye lo que venga del `.env`).
try:
    from app.core.config import get_settings

    _APP_DB_URL = get_settings().database_url
except Exception:  # pragma: no cover - si config no carga, seguimos con env
    _APP_DB_URL = os.getenv("DATABASE_URL", "")
if _APP_DB_URL:
    _assert_local(_APP_DB_URL, "DATABASE_URL (engine de la app)")

# 2. La DB dedicada de tests.
_assert_local(TEST_DATABASE_URL, "TEST_DATABASE_URL")
if TEST_DATABASE_URL.rstrip("/").endswith("/insyta") or (
    _APP_DB_URL and TEST_DATABASE_URL == _APP_DB_URL
):
    raise RuntimeError(
        "TEST_DATABASE_URL apunta a la DB de la app — abortando para no borrar "
        "datos. Usá una DB dedicada (ej. .../insyta_test)."
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
                        "(id, public_id, org_id, slug, name) "
                        "VALUES (:id, :pid, :oid, :slug, :name)"
                    ),
                    {
                        "id": pid,
                        "pid": f"proj_{pid.hex[:16]}",
                        "oid": oid,
                        "slug": slug,
                        "name": "Project",
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
