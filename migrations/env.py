"""Alembic environment.

Reads DATABASE_URL from app.core.config.get_settings() so the URL stays in one
place. Falls back to alembic.ini's `sqlalchemy.url` if settings can't be loaded
(e.g. running `alembic` without dependencies).
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.models import Base  # registers all models on Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _host(url: str) -> str:
    """Host de una URL sqlalchemy (postgresql+asyncpg://u:p@HOST:port/db)."""
    try:
        tail = url.split("@", 1)[1] if "@" in url else url.split("://", 1)[1]
        return tail.split("/", 1)[0].rsplit(":", 1)[0].strip("[]").lower()
    except (IndexError, AttributeError):
        return url


# Resolvemos la URL desde settings; el except SOLO cubre el fallo de carga de
# settings (CI sin config) — NO puede tragarse la guarda anti-prod de abajo.
settings_url: str | None = None
_is_prod_env = False
try:
    from app.core.config import get_settings

    # Alembic corre como el rol dueño (`postgres`): usa migration_database_url si
    # está seteada, si no cae a database_url (compat con setups sin split de rol).
    _s = get_settings()
    settings_url = _s.migration_database_url or _s.database_url
    _is_prod_env = (_s.environment or "").lower() == "production"
except Exception:  # pragma: no cover - settings unavailable in some CI contexts
    settings_url = None

if settings_url:
    # GUARDA ANTI-PROD (fuera del try, para que el raise NO se trague): correr una
    # migración local contra una DB REMOTA (Supabase/prod) por accidente es como se
    # metieron tablas de auditoría en prod. Fuera de environment=production abortamos
    # si el host es remoto, salvo opt-in explícito ALLOW_REMOTE_MIGRATION=1 (EC2/CI).
    _local = {"localhost", "127.0.0.1", "::1", "postgres", "db", ""}
    _opt_in = os.getenv("ALLOW_REMOTE_MIGRATION") == "1"
    if _host(settings_url) not in _local and not _is_prod_env and not _opt_in:
        raise RuntimeError(
            f"Alembic apunta a una DB REMOTA (host={_host(settings_url)!r}) fuera de "
            "environment=production. Abortando para no migrar prod por accidente. "
            "Si es intencional (deploy), seteá ALLOW_REMOTE_MIGRATION=1."
        )
    config.set_main_option("sqlalchemy.url", settings_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
