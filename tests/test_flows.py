"""Tests for flow endpoints (upload Langflow JSON, list, detail)."""

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

from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.core.db import get_db_with_tenant_context  # noqa: E402
from app.main import app  # noqa: E402

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/insyta",
)
RLS_ROLE = "insyta_rls_test"
RLS_PASSWORD = "rls-test-pw"

SAMPLE_FLOW = {
    "name": "FLOW AUTOMATION",
    "data": {
        "nodes": [
            {"id": "ChatInput-1", "data": {"type": "ChatInput"}},
            {"id": "Agent-1", "data": {"type": "Agent"}},
            {"id": "Agent-2", "data": {"type": "Agent"}},
            {"id": "Prompt-1", "data": {"type": "Prompt"}},
        ],
        "edges": [{"source": "ChatInput-1", "target": "Agent-1"}],
    },
}


async def _db_available() -> bool:
    eng = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with eng.connect() as c:
            r = await c.execute(
                text("SELECT 1 FROM information_schema.tables WHERE table_name='flows'")
            )
            return r.scalar_one_or_none() is not None
    except Exception:
        return False
    finally:
        await eng.dispose()


async def _ensure_rls_role(su: AsyncEngine) -> None:
    async with su.begin() as conn:
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


async def _seed_project(su: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID, str]:
    factory = async_sessionmaker(su, expire_on_commit=False)
    org_id = uuid.uuid4()
    proj_id = uuid.uuid4()
    proj_public = f"proj_{proj_id.hex[:16]}"
    async with factory() as s:
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
                    "INSERT INTO projects(id, public_id, org_id, slug, name) "
                    "VALUES (:id, :pid, :org, :slug, 'P')"
                ),
                {
                    "id": proj_id,
                    "pid": proj_public,
                    "org": org_id,
                    "slug": f"p-{proj_id.hex[:6]}",
                },
            )
    return org_id, proj_id, proj_public


async def _cleanup(su: AsyncEngine, org_id: uuid.UUID) -> None:
    async with su.begin() as conn:
        await conn.execute(
            text("DELETE FROM organizations WHERE id = :id"), {"id": org_id}
        )


def _make_token(org_id: uuid.UUID, projects: list[uuid.UUID]) -> str:
    return jwt.encode(
        {
            "sub": "test-user",
            "email": "test@insyta.space",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
            "org_id": str(org_id),
            "allowed_project_ids": [str(p) for p in projects],
        },
        "x" * 32,
        algorithm="HS256",
    )


def _override(rls: AsyncEngine, org_id: uuid.UUID, projects: list[uuid.UUID]):
    factory = async_sessionmaker(rls, expire_on_commit=False)

    async def _dep():
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

    return _dep


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "x" * 32)
    monkeypatch.setenv("ENVIRONMENT", "development")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_upload_list_and_get_flow():
    if not await _db_available():
        pytest.skip("flows table not present")
    su = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    await _ensure_rls_role(su)
    rls = create_async_engine(_rls_url(), pool_pre_ping=True)
    org_id, proj_id, proj_public = await _seed_project(su)
    try:
        app.dependency_overrides[get_db_with_tenant_context] = _override(
            rls, org_id, [proj_id]
        )
        token = _make_token(org_id, [proj_id])
        headers = {"Authorization": f"Bearer {token}"}
        async with _client() as c:
            # Upload
            r = await c.post(
                f"/api/v1/projects/{proj_public}/flows",
                headers=headers,
                json={
                    "name": "Bot Ventas",
                    "version": "1.4.0",
                    "flowJson": SAMPLE_FLOW,
                },
            )
            assert r.status_code == 201, r.text
            created = r.json()
            assert created["id"].startswith("flj_")
            assert created["agentCount"] == 2
            assert created["sizeBytes"] > 0
            flow_id = created["id"]

            # List
            r2 = await c.get(f"/api/v1/projects/{proj_public}/flows", headers=headers)
            assert r2.status_code == 200
            items = r2.json()
            assert len(items) == 1
            assert items[0]["agentCount"] == 2

            # Detail (includes json)
            r3 = await c.get(f"/api/v1/flows/{flow_id}", headers=headers)
            assert r3.status_code == 200
            detail = r3.json()
            assert "json" in detail
            assert "FLOW AUTOMATION" in detail["json"]
    finally:
        app.dependency_overrides.clear()
        await rls.dispose()
        await _cleanup(su, org_id)
        await su.dispose()
