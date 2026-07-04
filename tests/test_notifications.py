"""Servicio de notificaciones bajo RLS: crear (idempotente), listar, contar, marcar."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.services.notifications import (
    create_notification,
    list_notifications,
    mark_all_read,
    unread_count,
)


async def _session_for_org(engine: AsyncEngine, org_id):
    """Sesión con el GUC de tenant seteado (como get_db_with_tenant_context)."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    s = factory()
    await s.execute(
        text("SELECT set_config('app.current_org', :org, true)"),
        {"org": str(org_id)},
    )
    return s


@pytest.mark.asyncio
async def test_create_list_count_mark(postgres_engine, seeded_two_orgs):
    org = seeded_two_orgs["org_a"]
    proj = seeded_two_orgs["proj_a"]
    s = await _session_for_org(postgres_engine, org)
    try:
        await create_notification(
            s,
            org_id=org,
            project_id=proj,
            kind="audit",
            title="Auditoría completada",
            detail="Bot X · 3 conversaciones analizadas.",
            event_key="audit_completed:abc",
        )
        # Idempotente: mismo event_key no duplica.
        await create_notification(
            s,
            org_id=org,
            project_id=proj,
            kind="audit",
            title="Auditoría completada (dup)",
            event_key="audit_completed:abc",
        )
        await s.commit()

        items = await list_notifications(s, limit=10)
        assert len(items) == 1
        assert items[0].title == "Auditoría completada"
        assert await unread_count(s) == 1

        marked = await mark_all_read(s)
        await s.commit()
        assert marked == 1
        assert await unread_count(s) == 0
    finally:
        await s.execute(
            text("DELETE FROM notifications WHERE org_id = :o"), {"o": str(org)}
        )
        await s.commit()
        await s.close()
