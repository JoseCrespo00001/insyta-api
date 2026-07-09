"""Notifications endpoints — feed real de la campana del dashboard.

`GET /notifications` devuelve las últimas N + el contador sin leer.
`POST /notifications/read` marca todas como leídas (UX de la campana: al abrir,
el contador se limpia). RLS scopea por org vía `get_db_with_tenant_context`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db_with_tenant_context
from app.services.notifications import (
    list_notifications,
    mark_all_read,
    unread_count,
)

router = APIRouter(prefix="/api/v1", tags=["notifications"])


class _Camel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class NotificationOut(_Camel):
    id: str
    kind: str
    title: str
    detail: str | None
    link: str | None
    read: bool
    at: str


class NotificationsPage(_Camel):
    items: list[NotificationOut]
    unread_count: int


@router.get("/notifications", response_model=NotificationsPage)
async def get_notifications(
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> NotificationsPage:
    rows = await list_notifications(session, limit=limit)
    count = await unread_count(session)
    return NotificationsPage(
        items=[
            NotificationOut(
                id=n.public_id,
                kind=n.kind,
                title=n.title,
                detail=n.detail,
                link=n.link,
                read=n.is_read,
                at=n.created_at.isoformat(),
            )
            for n in rows
        ],
        unread_count=count,
    )


class MarkReadResult(_Camel):
    marked: int


@router.post("/notifications/read", response_model=MarkReadResult)
async def mark_notifications_read(
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> MarkReadResult:
    marked = await mark_all_read(session)
    await session.commit()
    return MarkReadResult(marked=marked)
