"""Servicio de notificaciones: crear (idempotente por event_key), listar,
contar sin leer, marcar leídas. Todas las queries corren bajo el contexto de
tenant (RLS) del caller — router (`get_db_with_tenant_context`) o worker
(`tenant_txn`). El filtro global de soft-delete ya excluye is_deleted=True.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification


async def create_notification(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    kind: str,
    title: str,
    project_id: uuid.UUID | None = None,
    detail: str | None = None,
    link: str | None = None,
    event_key: str | None = None,
) -> None:
    """Inserta una notificación. Si `event_key` está seteado, es idempotente:
    re-correr una auditoría no duplica la notificación (unique org_id+event_key)."""
    stmt = pg_insert(Notification).values(
        id=uuid.uuid4(),
        public_id=f"ntf_{uuid.uuid4().hex[:16]}",
        org_id=org_id,
        project_id=project_id,
        kind=kind,
        title=title,
        detail=detail,
        link=link,
        event_key=event_key,
    )
    if event_key is not None:
        stmt = stmt.on_conflict_do_nothing(constraint="uq_notifications_org_event")
    await session.execute(stmt)


async def list_notifications(
    session: AsyncSession, *, limit: int = 20
) -> list[Notification]:
    rows = (
        (
            await session.execute(
                select(Notification)
                .order_by(Notification.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def unread_count(session: AsyncSession) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(Notification)
            .where(Notification.is_read.is_(False))
        )
    ).scalar_one()


async def mark_all_read(session: AsyncSession) -> int:
    res = await session.execute(
        update(Notification).where(Notification.is_read.is_(False)).values(is_read=True)
    )
    return res.rowcount or 0
