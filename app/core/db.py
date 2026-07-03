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
from contextlib import asynccontextmanager

from fastapi import Depends
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

from app.core.auth import CurrentUser, get_current_user
from app.core.config import get_settings
from app.models.base import SoftDeleteMixin


def _build_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        future=True,
        # asyncpg + Supabase pooler (PgBouncer) can't reuse prepared statements
        # across pooled connections; disabling the statement cache makes the
        # engine work against the pooler and is harmless on direct connections.
        connect_args={"statement_cache_size": 0},
    )


engine: AsyncEngine = _build_engine()
async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)


@event.listens_for(Session, "do_orm_execute")
def _filter_soft_deleted(state: ORMExecuteState) -> None:
    """Soft-delete global: agrega `is_deleted = False` a todo SELECT ORM sobre
    cualquier modelo que herede de `SoftDeleteMixin`, así ninguna lectura
    devuelve filas borradas lógicamente sin tener que tocar los ~40 sitios de
    query a mano.

    Opt-out por query con `.execution_options(include_deleted=True)` para casos
    internos que sí necesitan ver todo (upsert idempotente, el propio
    soft-delete, jobs de mantenimiento). No aplica a UPDATE/DELETE ni a joins
    externos con agregados (esos se filtran explícitamente en el ON clause).
    """
    if not state.is_select:
        return
    if state.is_column_load or state.is_relationship_load:
        # cargas de relaciones/refresh de columnas: no re-aplicar el criterio
        return
    if state.execution_options.get("include_deleted", False):
        return
    state.statement = state.statement.options(
        with_loader_criteria(
            SoftDeleteMixin,
            lambda cls: cls.is_deleted.is_(False),
            include_aliases=True,
        )
    )


async def get_db() -> AsyncIterator[AsyncSession]:
    """System-level session (no tenant context — RLS sees NULL GUCs)."""
    async with async_session_factory() as session:
        yield session


async def get_db_with_tenant_context(
    current_user: CurrentUser = Depends(get_current_user),
) -> AsyncIterator[AsyncSession]:
    """Open a session with `app.current_org` + `app.allowed_projects` set for RLS.

    Uses `set_config(name, value, true)` — the `true` makes the binding
    transaction-local. We yield *inside* `session.begin()` so the GUCs survive
    until the request handler is done, and they vanish the moment the
    transaction closes. With pgbouncer in transaction-pooling mode the
    physical connection is released between transactions, so leaking
    session-level GUCs to the next tenant is impossible.

    Bound parameters guard against SQL injection even if `current_user`
    state is ever tainted upstream.
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


@asynccontextmanager
async def tenant_txn(
    org_id: uuid.UUID,
    allowed_project_ids: list[uuid.UUID] | None = None,
) -> AsyncIterator[AsyncSession]:
    """Open a session inside a transaction with the tenant GUCs set, for
    background workers / system ops. The transaction commits on clean exit.

    Setting `app.current_org` is what lets writes pass RLS WITH CHECK under
    FORCE RLS (Supabase, where the connection role is not a superuser).
    """
    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.current_org', :v, true)"),
                {"v": str(org_id)},
            )
            await session.execute(
                text("SELECT set_config('app.allowed_projects', :v, true)"),
                {"v": ",".join(str(p) for p in (allowed_project_ids or []))},
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
