"""End-to-end tests for /api/v1/projects/{id}/score, /conversations and detail.

Uses httpx.AsyncClient with ASGITransport so the engine and the request handler
run on the same asyncio loop (TestClient runs the app in a worker thread, which
detaches asyncpg connections from the test loop).
"""

from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest
from httpx import ASGITransport
from jose import jwt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.core.db import get_db_with_tenant_context
from app.main import app

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/insyta",
)

RLS_ROLE = "insyta_rls_test"
RLS_PASSWORD = "rls-test-pw"


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


async def _ensure_rls_role(superuser_engine: AsyncEngine) -> None:
    async with superuser_engine.begin() as conn:
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


def _rls_url() -> str:
    return TEST_DATABASE_URL.replace(
        "postgres:postgres@", f"{RLS_ROLE}:{RLS_PASSWORD}@"
    )


async def _seed_two_orgs(su_engine: AsyncEngine) -> dict:
    """Insert 2 orgs/projects/agents/conversations as the table owner."""
    factory = async_sessionmaker(su_engine, expire_on_commit=False)
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    proj_a = uuid.uuid4()
    proj_b = uuid.uuid4()
    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()
    conv_a = uuid.uuid4()
    conv_b = uuid.uuid4()
    proj_a_public = f"proj_{proj_a.hex[:16]}"
    proj_b_public = f"proj_{proj_b.hex[:16]}"
    conv_a_public = f"conv_{conv_a.hex[:16]}"
    conv_b_public = f"conv_{conv_b.hex[:16]}"

    async with factory() as s:
        async with s.begin():
            for oid, slug in [
                (org_a, f"org-a-{org_a.hex[:6]}"),
                (org_b, f"org-b-{org_b.hex[:6]}"),
            ]:
                await s.execute(
                    text(
                        "INSERT INTO organizations(id, public_id, slug, name) "
                        "VALUES (:id, :pid, :slug, 'Org')"
                    ),
                    {"id": oid, "pid": f"org_{oid.hex[:16]}", "slug": slug},
                )
            for pid, oid, pub, slug in [
                (proj_a, org_a, proj_a_public, f"proj-a-{proj_a.hex[:6]}"),
                (proj_b, org_b, proj_b_public, f"proj-b-{proj_b.hex[:6]}"),
            ]:
                await s.execute(
                    text(
                        "INSERT INTO projects(id, public_id, org_id, slug, name) "
                        "VALUES (:id, :pid, :oid, :slug, 'P')"
                    ),
                    {"id": pid, "pid": pub, "oid": oid, "slug": slug},
                )
            for aid, pid, oid in [(agent_a, proj_a, org_a), (agent_b, proj_b, org_b)]:
                await s.execute(
                    text(
                        "INSERT INTO agents(id, public_id, project_id, org_id, slug, name, platform) "
                        "VALUES (:id, :pid, :proj, :org, 'agent', 'A', 'custom_sdk')"
                    ),
                    {"id": aid, "pid": f"agt_{aid.hex[:16]}", "proj": pid, "org": oid},
                )
            for cid, pid, oid, aid, ext, pub in [
                (conv_a, proj_a, org_a, agent_a, "ext-a", conv_a_public),
                (conv_b, proj_b, org_b, agent_b, "ext-b", conv_b_public),
            ]:
                await s.execute(
                    text(
                        "INSERT INTO conversations"
                        "(id, public_id, project_id, org_id, agent_id, external_id, platform) "
                        "VALUES (:id, :pid, :proj, :org, :agent, :ext, 'custom_sdk')"
                    ),
                    {
                        "id": cid,
                        "pid": pub,
                        "proj": pid,
                        "org": oid,
                        "agent": aid,
                        "ext": ext,
                    },
                )

    return {
        "org_a": org_a,
        "org_b": org_b,
        "proj_a": proj_a,
        "proj_b": proj_b,
        "agent_a": agent_a,
        "agent_b": agent_b,
        "conv_a": conv_a,
        "conv_b": conv_b,
        "proj_a_public": proj_a_public,
        "proj_b_public": proj_b_public,
        "conv_a_public": conv_a_public,
        "conv_b_public": conv_b_public,
    }


async def _cleanup_orgs(su_engine: AsyncEngine, ids: dict) -> None:
    async with su_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM organizations WHERE id IN (:a, :b)"),
            {"a": ids["org_a"], "b": ids["org_b"]},
        )


def _make_token(org_id: uuid.UUID, allowed: list[uuid.UUID]) -> str:
    base = {
        "sub": "test-user",
        "email": "test@insyta.space",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "org_id": str(org_id),
        "allowed_project_ids": [str(p) for p in allowed],
    }
    return jwt.encode(base, "x" * 32, algorithm="HS256")


def _override_with_rls(
    rls_engine: AsyncEngine, org_id: uuid.UUID, projects: list[uuid.UUID]
):
    factory = async_sessionmaker(rls_engine, expire_on_commit=False)

    async def _override():
        async with factory() as s:
            async with s.begin():
                await s.execute(
                    text("SELECT set_config('app.current_org', :v, true)"),
                    {"v": str(org_id)},
                )
                await s.execute(
                    text("SELECT set_config('app.allowed_projects', :v, true)"),
                    {"v": ",".join(str(p) for p in projects)},
                )
                yield s

    return _override


@pytest.fixture(autouse=True)
def _settings_for_jwt(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "x" * 32)
    monkeypatch.setenv("ENVIRONMENT", "development")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_unauthenticated_returns_401():
    async with _client() as c:
        r = await c.get("/api/v1/projects/proj_xxx/score")
        assert r.status_code == 401
        r2 = await c.get("/api/v1/projects/proj_xxx/conversations")
        assert r2.status_code == 401
        r3 = await c.get("/api/v1/conversations/conv_xxx")
        assert r3.status_code == 401


@pytest.mark.asyncio
async def test_score_for_org_a_only_sees_own():
    if not await _db_available():
        pytest.skip("PostgreSQL test DB unavailable")
    su_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    await _ensure_rls_role(su_engine)
    rls_engine = create_async_engine(_rls_url(), pool_pre_ping=True)
    ids = await _seed_two_orgs(su_engine)
    try:
        app.dependency_overrides[get_db_with_tenant_context] = _override_with_rls(
            rls_engine, ids["org_a"], [ids["proj_a"]]
        )
        token = _make_token(ids["org_a"], [ids["proj_a"]])
        async with _client() as c:
            r = await c.get(
                f"/api/v1/projects/{ids['proj_a_public']}/score",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["project_public_id"] == ids["proj_a_public"]
        assert body["score"] is None
        assert body["evaluation_count"] == 0
    finally:
        app.dependency_overrides.clear()
        await rls_engine.dispose()
        await _cleanup_orgs(su_engine, ids)
        await su_engine.dispose()


@pytest.mark.asyncio
async def test_score_cross_tenant_returns_404():
    if not await _db_available():
        pytest.skip("PostgreSQL test DB unavailable")
    su_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    await _ensure_rls_role(su_engine)
    rls_engine = create_async_engine(_rls_url(), pool_pre_ping=True)
    ids = await _seed_two_orgs(su_engine)
    try:
        app.dependency_overrides[get_db_with_tenant_context] = _override_with_rls(
            rls_engine, ids["org_a"], [ids["proj_a"]]
        )
        token = _make_token(ids["org_a"], [ids["proj_a"]])
        async with _client() as c:
            r = await c.get(
                f"/api/v1/projects/{ids['proj_b_public']}/score",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()
        await rls_engine.dispose()
        await _cleanup_orgs(su_engine, ids)
        await su_engine.dispose()


@pytest.mark.asyncio
async def test_list_conversations_only_returns_own():
    if not await _db_available():
        pytest.skip("PostgreSQL test DB unavailable")
    su_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    await _ensure_rls_role(su_engine)
    rls_engine = create_async_engine(_rls_url(), pool_pre_ping=True)
    ids = await _seed_two_orgs(su_engine)
    try:
        app.dependency_overrides[get_db_with_tenant_context] = _override_with_rls(
            rls_engine, ids["org_a"], [ids["proj_a"]]
        )
        token = _make_token(ids["org_a"], [ids["proj_a"]])
        async with _client() as c:
            r = await c.get(
                f"/api/v1/projects/{ids['proj_a_public']}/conversations",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["public_id"] == ids["conv_a_public"]
    finally:
        app.dependency_overrides.clear()
        await rls_engine.dispose()
        await _cleanup_orgs(su_engine, ids)
        await su_engine.dispose()


@pytest.mark.asyncio
async def test_list_conversations_pagination_cursor():
    if not await _db_available():
        pytest.skip("PostgreSQL test DB unavailable")
    su_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    await _ensure_rls_role(su_engine)
    rls_engine = create_async_engine(_rls_url(), pool_pre_ping=True)
    ids = await _seed_two_orgs(su_engine)
    try:
        # Add 3 more conversations to proj_a
        async with su_engine.begin() as conn:
            for i in range(3):
                cid = uuid.uuid4()
                await conn.execute(
                    text(
                        "INSERT INTO conversations"
                        "(id, public_id, project_id, org_id, agent_id, external_id, platform) "
                        "VALUES (:id, :pid, :proj, :org, :agent, :ext, 'custom_sdk')"
                    ),
                    {
                        "id": cid,
                        "pid": f"conv_{cid.hex[:16]}",
                        "proj": ids["proj_a"],
                        "org": ids["org_a"],
                        "agent": ids["agent_a"],
                        "ext": f"ext-extra-{i}",
                    },
                )

        app.dependency_overrides[get_db_with_tenant_context] = _override_with_rls(
            rls_engine, ids["org_a"], [ids["proj_a"]]
        )
        token = _make_token(ids["org_a"], [ids["proj_a"]])
        async with _client() as c:
            r = await c.get(
                f"/api/v1/projects/{ids['proj_a_public']}/conversations?limit=2",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200
            page1 = r.json()
            assert len(page1["items"]) == 2
            assert page1["next_cursor"] is not None

            r2 = await c.get(
                f"/api/v1/projects/{ids['proj_a_public']}/conversations"
                f"?limit=2&cursor={page1['next_cursor']}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r2.status_code == 200
            page2 = r2.json()
            assert len(page2["items"]) >= 1
            seen = {i["public_id"] for i in page1["items"] + page2["items"]}
            assert len(seen) == len(page1["items"]) + len(page2["items"])
    finally:
        app.dependency_overrides.clear()
        await rls_engine.dispose()
        await _cleanup_orgs(su_engine, ids)
        await su_engine.dispose()


@pytest.mark.asyncio
async def test_conversation_detail_cross_tenant_returns_404():
    if not await _db_available():
        pytest.skip("PostgreSQL test DB unavailable")
    su_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    await _ensure_rls_role(su_engine)
    rls_engine = create_async_engine(_rls_url(), pool_pre_ping=True)
    ids = await _seed_two_orgs(su_engine)
    try:
        app.dependency_overrides[get_db_with_tenant_context] = _override_with_rls(
            rls_engine, ids["org_a"], [ids["proj_a"]]
        )
        token = _make_token(ids["org_a"], [ids["proj_a"]])
        async with _client() as c:
            r = await c.get(
                f"/api/v1/conversations/{ids['conv_b_public']}",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()
        await rls_engine.dispose()
        await _cleanup_orgs(su_engine, ids)
        await su_engine.dispose()


@pytest.mark.asyncio
async def test_conversation_detail_returns_messages_and_evaluation():
    if not await _db_available():
        pytest.skip("PostgreSQL test DB unavailable")
    su_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    await _ensure_rls_role(su_engine)
    rls_engine = create_async_engine(_rls_url(), pool_pre_ping=True)
    ids = await _seed_two_orgs(su_engine)
    msg_id = uuid.uuid4()
    eval_id = uuid.uuid4()
    try:
        async with su_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO messages"
                    "(id, public_id, conversation_id, project_id, org_id,"
                    " role, content, content_anonymized, timestamp) "
                    "VALUES (:id, :pid, :conv, :proj, :org, 'user',"
                    "        'hola', 'hola', now())"
                ),
                {
                    "id": msg_id,
                    "pid": f"msg_{msg_id.hex[:16]}",
                    "conv": ids["conv_a"],
                    "proj": ids["proj_a"],
                    "org": ids["org_a"],
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO evaluations"
                    "(id, public_id, conversation_id, project_id, org_id, agent_id,"
                    " score, resolution, satisfaction, tone, summary, evaluated_at) "
                    "VALUES (:id, :pid, :conv, :proj, :org, :agent,"
                    "        87, true, 4, 'positive', 'Resolved fast', now())"
                ),
                {
                    "id": eval_id,
                    "pid": f"eval_{eval_id.hex[:16]}",
                    "conv": ids["conv_a"],
                    "proj": ids["proj_a"],
                    "org": ids["org_a"],
                    "agent": ids["agent_a"],
                },
            )

        app.dependency_overrides[get_db_with_tenant_context] = _override_with_rls(
            rls_engine, ids["org_a"], [ids["proj_a"]]
        )
        token = _make_token(ids["org_a"], [ids["proj_a"]])
        async with _client() as c:
            r = await c.get(
                f"/api/v1/conversations/{ids['conv_a_public']}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200
            body = r.json()
            assert body["public_id"] == ids["conv_a_public"]
            assert len(body["messages"]) == 1
            assert body["messages"][0]["role"] == "user"
            # NOTE 2026-06-24: eval_to_camel (per-conversación) NO emite `score`
            # — el score es agregado (se expone a nivel proyecto/lista). Asertamos
            # los campos que sí serializa el detalle.
            assert body["evaluation"]["tone"] == "positive"
            assert body["evaluation"]["resolution"] is True
            assert body["evaluation"]["satisfaction"] == 4

            r2 = await c.get(
                f"/api/v1/projects/{ids['proj_a_public']}/score",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r2.status_code == 200
            assert r2.json()["score"] == 87
            assert r2.json()["evaluation_count"] == 1
    finally:
        app.dependency_overrides.clear()
        await rls_engine.dispose()
        await _cleanup_orgs(su_engine, ids)
        await su_engine.dispose()
