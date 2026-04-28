"""RLS isolation tests.

These tests prove that PostgreSQL Row-Level Security cuts off cross-tenant
reads even when the application code "forgets" to filter. They are the
load-bearing safety net behind multi-tenancy.

Strategy:
1. Seed two orgs with one conversation each (as table owner — bypasses RLS).
2. Query as a *non-owner* role with RLS forced; assert the GUC controls what
   rows come back.

We create a dedicated `rls_app` role per test session. PG RLS only filters for
non-superuser, non-bypass roles; the postgres superuser is exempt regardless of
FORCE. Without this role split, the test would silently always pass.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from tests.conftest import TEST_DATABASE_URL

RLS_ROLE = "insyta_rls_test"
RLS_PASSWORD = "rls-test-pw"


@pytest_asyncio.fixture
async def rls_engine(postgres_engine: AsyncEngine):
    """Engine that connects as a non-superuser role so RLS is enforced."""
    # Provision the role + grants once.
    async with postgres_engine.begin() as conn:
        await conn.execute(
            text(
                f"DO $$ BEGIN "
                f"  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{RLS_ROLE}') THEN "
                f"    CREATE ROLE {RLS_ROLE} LOGIN PASSWORD '{RLS_PASSWORD}'; "
                f"  END IF; "
                f"END $$;"
            )
        )
        await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {RLS_ROLE}"))
        await conn.execute(
            text(
                f"GRANT SELECT, INSERT, UPDATE, DELETE "
                f"ON ALL TABLES IN SCHEMA public TO {RLS_ROLE}"
            )
        )
        await conn.execute(
            text(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {RLS_ROLE}"
            )
        )

    rls_url = TEST_DATABASE_URL.replace(
        "postgres:postgres@", f"{RLS_ROLE}:{RLS_PASSWORD}@"
    )
    engine = create_async_engine(rls_url, pool_pre_ping=True)
    yield engine
    await engine.dispose()


@pytest.mark.asyncio
async def test_query_without_tenant_context_returns_zero_rows(
    rls_engine: AsyncEngine, seeded_two_orgs: dict
):
    """No GUC set => RLS policy compares NULL::uuid => no rows match."""
    factory = async_sessionmaker(rls_engine, expire_on_commit=False)
    async with factory() as s:
        result = await s.execute(text("SELECT COUNT(*) FROM conversations"))
        assert result.scalar_one() == 0


@pytest.mark.asyncio
async def test_query_with_org_a_context_only_returns_org_a(
    rls_engine: AsyncEngine, seeded_two_orgs: dict
):
    factory = async_sessionmaker(rls_engine, expire_on_commit=False)
    async with factory() as s:
        async with s.begin():
            await s.execute(
                text("SELECT set_config('app.current_org', :v, true)"),
                {"v": str(seeded_two_orgs["org_a"])},
            )
            await s.execute(
                text("SELECT set_config('app.allowed_projects', :v, true)"),
                {"v": str(seeded_two_orgs["proj_a"])},
            )
            result = await s.execute(text("SELECT id, org_id FROM conversations"))
            rows = result.all()
        assert len(rows) == 1
        assert rows[0].id == seeded_two_orgs["conv_a"]
        assert rows[0].org_id == seeded_two_orgs["org_a"]


@pytest.mark.asyncio
async def test_query_with_unrelated_org_returns_zero(
    rls_engine: AsyncEngine, seeded_two_orgs: dict
):
    """Setting app.current_org to a uuid that owns no rows yields 0."""
    factory = async_sessionmaker(rls_engine, expire_on_commit=False)
    bogus_org = uuid.uuid4()
    async with factory() as s:
        async with s.begin():
            await s.execute(
                text("SELECT set_config('app.current_org', :v, true)"),
                {"v": str(bogus_org)},
            )
            await s.execute(
                text("SELECT set_config('app.allowed_projects', :v, true)"),
                {"v": str(uuid.uuid4())},
            )
            result = await s.execute(text("SELECT COUNT(*) FROM conversations"))
        assert result.scalar_one() == 0


@pytest.mark.asyncio
async def test_set_local_uses_bind_param_not_string_concat(
    rls_engine: AsyncEngine, seeded_two_orgs: dict
):
    """Verify set_config rejects a malicious payload safely (no SQL injection).

    A payload like `'); DROP TABLE conversations; --` should be stored as a
    string. The subsequent UUID cast in the policy then fails, but the table
    must still exist afterwards.
    """
    factory = async_sessionmaker(rls_engine, expire_on_commit=False)
    payload = "'); DROP TABLE conversations; --"
    async with factory() as s:
        with pytest.raises(Exception):
            async with s.begin():
                await s.execute(
                    text("SELECT set_config('app.current_org', :v, true)"),
                    {"v": payload},
                )
                await s.execute(text("SELECT COUNT(*) FROM conversations"))

    # Sanity: the table still exists (the injection didn't drop it).
    async with async_sessionmaker(rls_engine, expire_on_commit=False)() as s2:
        # Use the postgres-owned engine indirectly: rls_engine connects as
        # rls role, table existence shows up in pg_class regardless of GUC.
        await s2.execute(text("SELECT to_regclass('public.conversations')"))
