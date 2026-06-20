"""Alembic migration round-trip test (EQUIP-103).

Drives `alembic upgrade head` -> `alembic downgrade base` -> `alembic upgrade
head` against the docker-compose Postgres. Skips cleanly if the DB is not
available so CI without docker doesn't break.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import TEST_DATABASE_URL

REPO_ROOT = Path(__file__).resolve().parents[1]


def _alembic_available() -> bool:
    return (
        shutil.which("alembic") is not None
        or (REPO_ROOT / ".venv" / "bin" / "alembic").exists()
    )


def _alembic(args: list[str]) -> subprocess.CompletedProcess[str]:
    bin_path = REPO_ROOT / ".venv" / "bin" / "alembic"
    cmd = [str(bin_path) if bin_path.exists() else "alembic", *args]
    env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL}
    return subprocess.run(
        cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=False, env=env
    )


@pytest.mark.asyncio
async def test_db_reachable():
    eng = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with eng.connect() as conn:
            r = await conn.execute(text("SELECT 1"))
            assert r.scalar_one() == 1
    except Exception:
        pytest.skip("PostgreSQL test DB unavailable")
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_alembic_round_trip():
    if not _alembic_available():
        pytest.skip("alembic not on PATH and not installed in .venv")

    # Ensure DB exists.
    eng = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with eng.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("PostgreSQL test DB unavailable")
    finally:
        await eng.dispose()

    # 1. Down to base.
    proc = _alembic(["downgrade", "base"])
    assert proc.returncode == 0, proc.stderr or proc.stdout

    # 2. Up to head.
    proc = _alembic(["upgrade", "head"])
    assert proc.returncode == 0, proc.stderr or proc.stdout

    # 3. Down to base again (round-trip).
    proc = _alembic(["downgrade", "base"])
    assert proc.returncode == 0, proc.stderr or proc.stdout

    # 4. Up to head once more so the rest of the suite has a populated schema.
    proc = _alembic(["upgrade", "head"])
    assert proc.returncode == 0, proc.stderr or proc.stdout

    # Verify expected tables exist after final upgrade.
    eng = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with eng.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname='public' AND tablename != 'alembic_version'"
                )
            )
            tables = {row[0] for row in result.all()}
        expected = {
            "organizations",
            "projects",
            "agents",
            "uploads",
            "conversations",
            "messages",
            "evaluations",
            "users",
            "flows",
            "audits",
            "audit_conversations",
            "message_evaluations",
            "improvements",
            "improvement_conversations",
        }
        assert expected.issubset(tables), tables
    finally:
        await eng.dispose()
