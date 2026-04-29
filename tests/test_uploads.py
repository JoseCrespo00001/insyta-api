"""Upload tests.

Validation paths (size, extension, auth) are covered with httpx.AsyncClient and
a fake DB session. The DB-integration test runs only if migration 0002_uploads
has been applied to the test DB.
"""

from __future__ import annotations

import io
import os
import time
import uuid
from collections.abc import AsyncIterator

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
from app.services import celery_app as celery_app_module

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/insyta",
)
RLS_ROLE = "insyta_rls_test"
RLS_PASSWORD = "rls-test-pw"


@pytest.fixture(autouse=True)
def _settings_for_jwt(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "x" * 32)
    monkeypatch.setenv("ENVIRONMENT", "development")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _stub_celery(monkeypatch):
    """Replace celery_app.send_task with a recorder so we don't hit a broker."""
    calls: list[tuple] = []

    def fake_send_task(name: str, args=None, kwargs=None, **opts):
        calls.append((name, args, kwargs))

    monkeypatch.setattr(celery_app_module.celery_app, "send_task", fake_send_task)
    yield calls


def _make_token(org_id: uuid.UUID, allowed: list[uuid.UUID]) -> str:
    base = {
        "sub": "test-user",
        "email": "test@insyta.io",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "org_id": str(org_id),
        "allowed_project_ids": [str(p) for p in allowed],
    }
    return jwt.encode(base, "x" * 32, algorithm="HS256")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _db_available() -> bool:
    eng = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with eng.connect() as c:
            res = await c.execute(text("SELECT to_regclass('public.uploads')"))
            return res.scalar_one() is not None
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


async def _seed_org_and_project(su_engine: AsyncEngine) -> dict:
    factory = async_sessionmaker(su_engine, expire_on_commit=False)
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
            from cryptography.fernet import Fernet

            test_ws = Fernet(Fernet.generate_key()).encrypt(b"test-secret")
            await s.execute(
                text(
                    "INSERT INTO projects"
                    "(id, public_id, org_id, slug, name, webhook_secret_encrypted) "
                    "VALUES (:id, :pid, :org, 'p', 'P', :ws)"
                ),
                {
                    "id": proj_id,
                    "pid": proj_public,
                    "org": org_id,
                    "ws": test_ws,
                },
            )
    return {"org_id": org_id, "proj_id": proj_id, "proj_public": proj_public}


async def _cleanup(su_engine: AsyncEngine, ids: dict) -> None:
    async with su_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM organizations WHERE id = :id"),
            {"id": ids["org_id"]},
        )


# ---------------- validation paths (no DB) ----------------


@pytest.mark.asyncio
async def test_unauth_returns_401():
    async with _client() as c:
        r = await c.post("/api/v1/uploads/csv")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_unauth_returns_401():
    async with _client() as c:
        r = await c.get("/api/v1/uploads/upl_xxx")
        assert r.status_code == 401


# ---------------- DB-backed paths ----------------


@pytest.mark.asyncio
async def test_csv_with_wrong_extension_returns_400(_stub_celery):
    if not await _db_available():
        pytest.skip("uploads table not migrated yet")
    su = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    await _ensure_rls_role(su)
    rls = create_async_engine(_rls_url(), pool_pre_ping=True)
    ids = await _seed_org_and_project(su)
    try:
        app.dependency_overrides[get_db_with_tenant_context] = _override_with_rls(
            rls, ids["org_id"], [ids["proj_id"]]
        )
        token = _make_token(ids["org_id"], [ids["proj_id"]])
        async with _client() as c:
            r = await c.post(
                "/api/v1/uploads/csv",
                headers={"Authorization": f"Bearer {token}"},
                data={"project_public_id": ids["proj_public"]},
                files={"file": ("data.txt", io.BytesIO(b"hello"), "text/plain")},
            )
        assert r.status_code == 400
        assert ".csv" in r.json()["detail"]
        assert _stub_celery == []
    finally:
        app.dependency_overrides.clear()
        await rls.dispose()
        await _cleanup(su, ids)
        await su.dispose()


@pytest.mark.asyncio
async def test_csv_too_large_returns_413(_stub_celery, monkeypatch):
    if not await _db_available():
        pytest.skip("uploads table not migrated yet")
    # Shrink the cap so we don't allocate 50 MB.
    from app.routers import uploads as uploads_module

    monkeypatch.setattr(uploads_module, "MAX_UPLOAD_BYTES", 100)

    su = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    await _ensure_rls_role(su)
    rls = create_async_engine(_rls_url(), pool_pre_ping=True)
    ids = await _seed_org_and_project(su)
    try:
        app.dependency_overrides[get_db_with_tenant_context] = _override_with_rls(
            rls, ids["org_id"], [ids["proj_id"]]
        )
        token = _make_token(ids["org_id"], [ids["proj_id"]])
        oversized = b"a,b,c\n" * 200
        async with _client() as c:
            r = await c.post(
                "/api/v1/uploads/csv",
                headers={"Authorization": f"Bearer {token}"},
                data={"project_public_id": ids["proj_public"]},
                files={"file": ("data.csv", io.BytesIO(oversized), "text/csv")},
            )
        assert r.status_code == 413
        assert _stub_celery == []
    finally:
        app.dependency_overrides.clear()
        await rls.dispose()
        await _cleanup(su, ids)
        await su.dispose()


@pytest.mark.asyncio
async def test_csv_unknown_project_returns_404(_stub_celery):
    if not await _db_available():
        pytest.skip("uploads table not migrated yet")
    su = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    await _ensure_rls_role(su)
    rls = create_async_engine(_rls_url(), pool_pre_ping=True)
    ids = await _seed_org_and_project(su)
    try:
        app.dependency_overrides[get_db_with_tenant_context] = _override_with_rls(
            rls, ids["org_id"], [ids["proj_id"]]
        )
        token = _make_token(ids["org_id"], [ids["proj_id"]])
        async with _client() as c:
            r = await c.post(
                "/api/v1/uploads/csv",
                headers={"Authorization": f"Bearer {token}"},
                data={"project_public_id": "proj_does_not_exist"},
                files={"file": ("d.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")},
            )
        assert r.status_code == 404
        assert _stub_celery == []
    finally:
        app.dependency_overrides.clear()
        await rls.dispose()
        await _cleanup(su, ids)
        await su.dispose()


@pytest.mark.asyncio
async def test_csv_happy_path_returns_202_and_enqueues(_stub_celery):
    if not await _db_available():
        pytest.skip("uploads table not migrated yet")
    su = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    await _ensure_rls_role(su)
    rls = create_async_engine(_rls_url(), pool_pre_ping=True)
    ids = await _seed_org_and_project(su)
    try:
        app.dependency_overrides[get_db_with_tenant_context] = _override_with_rls(
            rls, ids["org_id"], [ids["proj_id"]]
        )
        token = _make_token(ids["org_id"], [ids["proj_id"]])
        body = b"external_id,role,content\nc1,user,hola\nc1,assistant,hi\n"
        async with _client() as c:
            r = await c.post(
                "/api/v1/uploads/csv",
                headers={"Authorization": f"Bearer {token}"},
                data={"project_public_id": ids["proj_public"]},
                files={"file": ("d.csv", io.BytesIO(body), "text/csv")},
            )
        assert r.status_code == 202
        body_json = r.json()
        assert body_json["status"] == "pending"
        upload_id = body_json["upload_id"]
        assert upload_id.startswith("upl_")
        assert len(_stub_celery) == 1
        name, args, _ = _stub_celery[0]
        assert name == "app.workers.processor.process_upload"
        assert isinstance(args[0], str)

        # GET polling returns the same row.
        async with _client() as c:
            poll = await c.get(
                f"/api/v1/uploads/{upload_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert poll.status_code == 200
        assert poll.json()["status"] == "pending"
        assert poll.json()["project_public_id"] == ids["proj_public"]
    finally:
        app.dependency_overrides.clear()
        await rls.dispose()
        await _cleanup(su, ids)
        await su.dispose()


@pytest.mark.asyncio
async def test_get_unknown_upload_returns_404():
    if not await _db_available():
        pytest.skip("uploads table not migrated yet")
    su = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    await _ensure_rls_role(su)
    rls = create_async_engine(_rls_url(), pool_pre_ping=True)
    ids = await _seed_org_and_project(su)
    try:
        app.dependency_overrides[get_db_with_tenant_context] = _override_with_rls(
            rls, ids["org_id"], [ids["proj_id"]]
        )
        token = _make_token(ids["org_id"], [ids["proj_id"]])
        async with _client() as c:
            r = await c.get(
                "/api/v1/uploads/upl_does_not_exist",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()
        await rls.dispose()
        await _cleanup(su, ids)
        await su.dispose()
