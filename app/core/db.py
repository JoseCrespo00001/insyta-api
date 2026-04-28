"""Async SQLAlchemy engine + session factory + tenant-aware dependency.

`get_db_with_tenant_context` opens a session, sets the per-transaction GUCs
(`app.current_org`, `app.allowed_projects`) that PostgreSQL RLS reads, then
yields the session. Both GUCs are set via `set_config(name, value, true)` with
bound parameters — never via string concatenation — to keep the SQL injection
surface zero even if `current_user` is somehow tainted upstream.

The fallback `get_db` (no tenant) is for system-level queries such as
authentication lookups (`users` table) that need to bypass RLS.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.auth import CurrentUser, get_current_user
from app.core.config import get_settings


def _build_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        future=True,
    )


engine: AsyncEngine = _build_engine()
async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """System-level session (no tenant context — RLS sees NULL GUCs)."""
    async with async_session_factory() as session:
        yield session


async def get_db_with_tenant_context(
    current_user: CurrentUser = Depends(get_current_user),
) -> AsyncIterator[AsyncSession]:
    """Open a session with `app.current_org` + `app.allowed_projects` set for RLS.

    Uses `set_config(name, value, true)` (the `true` makes it transaction-local)
    with parameter binds. RLS policies read these GUCs to filter rows.
    """
    org_id_str = str(current_user.org_id)
    allowed = ",".join(str(p) for p in current_user.allowed_project_ids)

    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.current_org', :v, true)"),
                {"v": org_id_str},
            )
            await session.execute(
                text("SELECT set_config('app.allowed_projects', :v, true)"),
                {"v": allowed},
            )
        yield session


async def set_tenant_context(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    allowed_project_ids: list[uuid.UUID],
) -> None:
    """Manually set tenant GUCs on an already-open session (e.g. tests)."""
    await session.execute(
        text("SELECT set_config('app.current_org', :v, true)"),
        {"v": str(org_id)},
    )
    await session.execute(
        text("SELECT set_config('app.allowed_projects', :v, true)"),
        {"v": ",".join(str(p) for p in allowed_project_ids)},
    )
