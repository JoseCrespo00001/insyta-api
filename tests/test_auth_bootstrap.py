"""Bootstrap flow: a Supabase JWT (sub/email, no org) provisions org+user,
then /me resolves the org from the users table."""

from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest
from httpx import ASGITransport
from jose import jwt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.main import app  # noqa: E402

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/insyta",
)


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


def _token(sub: str, email: str) -> str:
    return jwt.encode(
        {
            "sub": sub,
            "email": email,
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        },
        "x" * 32,
        algorithm="HS256",
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "x" * 32)
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_bootstrap_then_me():
    if not await _db_available():
        pytest.skip("DB unavailable")
    sub = f"sub-{uuid.uuid4().hex[:12]}"
    email = f"{uuid.uuid4().hex[:8]}@insyta.io"
    token = _token(sub, email)
    headers = {"Authorization": f"Bearer {token}"}

    eng = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            # Without bootstrap, /me is 403 (no org).
            r0 = await c.get("/api/v1/me", headers=headers)
            assert r0.status_code == 403

            # Bootstrap provisions org+user.
            r1 = await c.post("/api/v1/auth/bootstrap", headers=headers)
            assert r1.status_code == 200, r1.text
            body = r1.json()
            assert body["created"] is True
            org_id = body["org_id"]

            # Idempotent.
            r2 = await c.post("/api/v1/auth/bootstrap", headers=headers)
            assert r2.json()["created"] is False

            # Now /me resolves the org from the users table.
            r3 = await c.get("/api/v1/me", headers=headers)
            assert r3.status_code == 200
            assert r3.json()["orgId"] == org_id
    finally:
        async with eng.begin() as conn:
            await conn.execute(
                text("DELETE FROM users WHERE supabase_user_id = :s"), {"s": sub}
            )
            await conn.execute(
                text(
                    "DELETE FROM organizations WHERE id IN "
                    "(SELECT org_id FROM users WHERE supabase_user_id = :s)"
                ),
                {"s": sub},
            )
        await eng.dispose()
