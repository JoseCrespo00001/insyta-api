"""End-to-end tests para /api/v1/settings/llm-keys.

Endpoint que estaba 0% cubierto (audit 2026-06-24). Prueba el ciclo de vida de las
API keys de proveedor (cifradas Fernet): set → get (masked) → delete, más los
error-paths: provider inválido (404), key corta (422) y key cifrada inválida
(configured=False, fail-safe del decrypt).
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

get_settings.cache_clear()

from app.core.db import get_db_with_tenant_context  # noqa: E402
from app.main import app  # noqa: E402
from app.services import secret_crypto  # noqa: E402

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
                    "WHERE table_name='organizations' "
                    "AND column_name='anthropic_api_key_encrypted'"
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


async def _set_raw_encrypted(su: AsyncEngine, org_id: uuid.UUID, value: str) -> None:
    async with su.begin() as conn:
        await conn.execute(
            text(
                "UPDATE organizations SET anthropic_api_key_encrypted = :v "
                "WHERE id = :id"
            ),
            {"v": value, "id": org_id},
        )


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


def _override_with_rls(rls: AsyncEngine, org_id: uuid.UUID):
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
                    {"v": ""},
                )
                yield s

    return _override


@pytest.fixture(autouse=True)
def _settings_for_jwt(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "x" * 32)
    monkeypatch.setenv("ENVIRONMENT", "development")
    get_settings.cache_clear()
    secret_crypto._fernet.cache_clear()
    yield
    get_settings.cache_clear()
    secret_crypto._fernet.cache_clear()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_unauthenticated_returns_401():
    async with _client() as c:
        r = await c.get("/api/v1/settings/llm-keys")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_set_get_delete_lifecycle():
    if not await _db_available():
        pytest.skip("schema not applied / org llm-key columns missing")
    su = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    await _ensure_rls_role(su)
    rls = create_async_engine(_rls_url(), pool_pre_ping=True)
    org_id = await _seed_org(su)
    try:
        app.dependency_overrides[get_db_with_tenant_context] = _override_with_rls(
            rls, org_id
        )
        token = _make_token(org_id)
        headers = {"Authorization": f"Bearer {token}"}
        async with _client() as c:
            # Estado inicial: no configurada.
            r = await c.get("/api/v1/settings/llm-keys", headers=headers)
            assert r.status_code == 200
            anthropic = next(k for k in r.json() if k["provider"] == "anthropic")
            assert anthropic["configured"] is False

            # PUT key → configured + masked, sin revelar la key.
            r = await c.put(
                "/api/v1/settings/llm-keys/anthropic",
                headers=headers,
                json={"apiKey": "sk-ant-api03-supersecreta-1234"},
            )
            assert r.status_code == 200
            body = r.json()
            assert body["configured"] is True
            assert "supersecreta" not in (body["masked"] or "")

            # GET refleja configured=True.
            r = await c.get("/api/v1/settings/llm-keys", headers=headers)
            anthropic = next(k for k in r.json() if k["provider"] == "anthropic")
            assert anthropic["configured"] is True

            # DELETE → vuelve a no configurada.
            r = await c.delete("/api/v1/settings/llm-keys/anthropic", headers=headers)
            assert r.status_code == 200
            assert r.json()["configured"] is False
    finally:
        app.dependency_overrides.clear()
        await _cleanup_org(su, org_id)
        await rls.dispose()
        await su.dispose()


@pytest.mark.asyncio
async def test_invalid_provider_returns_404():
    if not await _db_available():
        pytest.skip("schema not applied")
    su = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    await _ensure_rls_role(su)
    rls = create_async_engine(_rls_url(), pool_pre_ping=True)
    org_id = await _seed_org(su)
    try:
        app.dependency_overrides[get_db_with_tenant_context] = _override_with_rls(
            rls, org_id
        )
        token = _make_token(org_id)
        async with _client() as c:
            r = await c.put(
                "/api/v1/settings/llm-keys/no-existe",
                headers={"Authorization": f"Bearer {token}"},
                json={"apiKey": "sk-ant-api03-valida-1234"},
            )
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()
        await _cleanup_org(su, org_id)
        await rls.dispose()
        await su.dispose()


@pytest.mark.asyncio
async def test_short_key_returns_422():
    if not await _db_available():
        pytest.skip("schema not applied")
    su = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    await _ensure_rls_role(su)
    rls = create_async_engine(_rls_url(), pool_pre_ping=True)
    org_id = await _seed_org(su)
    try:
        app.dependency_overrides[get_db_with_tenant_context] = _override_with_rls(
            rls, org_id
        )
        token = _make_token(org_id)
        async with _client() as c:
            r = await c.put(
                "/api/v1/settings/llm-keys/anthropic",
                headers={"Authorization": f"Bearer {token}"},
                json={"apiKey": "short"},
            )
        assert r.status_code == 422
    finally:
        app.dependency_overrides.clear()
        await _cleanup_org(su, org_id)
        await rls.dispose()
        await su.dispose()


@pytest.mark.asyncio
async def test_corrupt_encrypted_key_reads_as_not_configured():
    """Fail-safe: si el valor cifrado en DB no es un token Fernet válido,
    decrypt_secret devuelve None y el endpoint reporta configured=False en vez
    de crashear o filtrar basura."""
    if not await _db_available():
        pytest.skip("schema not applied")
    su = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    await _ensure_rls_role(su)
    rls = create_async_engine(_rls_url(), pool_pre_ping=True)
    org_id = await _seed_org(su)
    try:
        await _set_raw_encrypted(su, org_id, "esto-no-es-un-token-fernet")
        app.dependency_overrides[get_db_with_tenant_context] = _override_with_rls(
            rls, org_id
        )
        token = _make_token(org_id)
        async with _client() as c:
            r = await c.get(
                "/api/v1/settings/llm-keys",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 200
        anthropic = next(k for k in r.json() if k["provider"] == "anthropic")
        assert anthropic["configured"] is False
        assert anthropic["masked"] is None
    finally:
        app.dependency_overrides.clear()
        await _cleanup_org(su, org_id)
        await rls.dispose()
        await su.dispose()
