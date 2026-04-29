"""End-to-end tests for POST /api/v1/projects with Fernet-encrypted secrets."""

from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport
from jose import jwt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

# Set the Fernet key BEFORE the FastAPI app or webhook_secret service load.
_FERNET_KEY = Fernet.generate_key().decode()
os.environ.setdefault("WEBHOOK_SECRET_KEY", _FERNET_KEY)

from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.core.db import get_db_with_tenant_context  # noqa: E402
from app.main import app  # noqa: E402
from app.services import webhook_secret as webhook_secret_module  # noqa: E402

webhook_secret_module._fernet.cache_clear()

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
            res = await c.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='projects' AND column_name='webhook_secret_encrypted'"
                )
            )
            return res.scalar_one_or_none() is not None
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


async def _seed_org(su: AsyncEngine) -> uuid.UUID:
    factory = async_sessionmaker(su, expire_on_commit=False)
    org_id = uuid.uuid4()
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
    return org_id


async def _cleanup_org(su: AsyncEngine, org_id: uuid.UUID) -> None:
    async with su.begin() as conn:
        await conn.execute(
            text("DELETE FROM organizations WHERE id = :id"), {"id": org_id}
        )


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


def _override_with_rls(rls: AsyncEngine, org_id: uuid.UUID, projects: list[uuid.UUID]):
    factory = async_sessionmaker(rls, expire_on_commit=False)

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
    monkeypatch.setenv("WEBHOOK_SECRET_KEY", _FERNET_KEY)
    get_settings.cache_clear()
    webhook_secret_module._fernet.cache_clear()
    yield
    get_settings.cache_clear()
    webhook_secret_module._fernet.cache_clear()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_unauthenticated_returns_401():
    async with _client() as c:
        r = await c.post("/api/v1/projects", json={"name": "X", "slug": "x"})
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_create_project_returns_raw_secret_and_persists_encrypted():
    if not await _db_available():
        pytest.skip("migration 0003 not applied")
    su = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    await _ensure_rls_role(su)
    rls = create_async_engine(_rls_url(), pool_pre_ping=True)
    org_id = await _seed_org(su)
    try:
        app.dependency_overrides[get_db_with_tenant_context] = _override_with_rls(
            rls, org_id, []
        )
        token = _make_token(org_id)
        async with _client() as c:
            r = await c.post(
                "/api/v1/projects",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "name": "Bot Ventas Q1",
                    "slug": "bot-ventas-q1",
                    "description": "Sales bot",
                },
            )
        assert r.status_code == 201
        body = r.json()
        assert body["name"] == "Bot Ventas Q1"
        assert body["slug"] == "bot-ventas-q1"
        assert body["public_id"].startswith("proj_")
        raw_secret = body["webhook_secret"]
        assert len(raw_secret) >= 40

        # The DB column stores ciphertext, not the raw secret.
        async with su.connect() as conn:
            row = await conn.execute(
                text(
                    "SELECT webhook_secret_encrypted FROM projects "
                    "WHERE public_id = :pid"
                ),
                {"pid": body["public_id"]},
            )
            stored = row.scalar_one()
        assert isinstance(stored, (bytes, bytearray, memoryview))
        stored_bytes = bytes(stored)
        assert raw_secret.encode() not in stored_bytes
        # Decryption with the same key recovers the original.
        decrypted = webhook_secret_module.decrypt_webhook_secret(stored_bytes)
        assert decrypted == raw_secret
    finally:
        app.dependency_overrides.clear()
        await rls.dispose()
        await _cleanup_org(su, org_id)
        await su.dispose()


@pytest.mark.asyncio
async def test_duplicate_slug_in_same_org_returns_409():
    if not await _db_available():
        pytest.skip("migration 0003 not applied")
    su = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    await _ensure_rls_role(su)
    rls = create_async_engine(_rls_url(), pool_pre_ping=True)
    org_id = await _seed_org(su)
    try:
        app.dependency_overrides[get_db_with_tenant_context] = _override_with_rls(
            rls, org_id, []
        )
        token = _make_token(org_id)
        async with _client() as c:
            r1 = await c.post(
                "/api/v1/projects",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "First", "slug": "dup"},
            )
            assert r1.status_code == 201
            r2 = await c.post(
                "/api/v1/projects",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "Second", "slug": "dup"},
            )
        assert r2.status_code == 409
        assert "already exists" in r2.json()["detail"]
    finally:
        app.dependency_overrides.clear()
        await rls.dispose()
        await _cleanup_org(su, org_id)
        await su.dispose()


@pytest.mark.asyncio
async def test_invalid_slug_returns_422():
    async with _client() as c:
        token = _make_token(uuid.uuid4())
        r = await c.post(
            "/api/v1/projects",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "X", "slug": "Has Spaces"},
        )
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_two_orgs_can_use_same_slug():
    if not await _db_available():
        pytest.skip("migration 0003 not applied")
    su = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    await _ensure_rls_role(su)
    rls = create_async_engine(_rls_url(), pool_pre_ping=True)
    org_a = await _seed_org(su)
    org_b = await _seed_org(su)
    try:
        token_a = _make_token(org_a)
        token_b = _make_token(org_b)

        app.dependency_overrides[get_db_with_tenant_context] = _override_with_rls(
            rls, org_a, []
        )
        async with _client() as c:
            r_a = await c.post(
                "/api/v1/projects",
                headers={"Authorization": f"Bearer {token_a}"},
                json={"name": "A", "slug": "shared-slug"},
            )
        assert r_a.status_code == 201

        app.dependency_overrides[get_db_with_tenant_context] = _override_with_rls(
            rls, org_b, []
        )
        async with _client() as c:
            r_b = await c.post(
                "/api/v1/projects",
                headers={"Authorization": f"Bearer {token_b}"},
                json={"name": "B", "slug": "shared-slug"},
            )
        assert r_b.status_code == 201
    finally:
        app.dependency_overrides.clear()
        await rls.dispose()
        await _cleanup_org(su, org_a)
        await _cleanup_org(su, org_b)
        await su.dispose()
