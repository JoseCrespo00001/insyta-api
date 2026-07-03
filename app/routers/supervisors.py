"""Supervisor endpoints — CRUD del cerebro reusable de auditoría.

Un Supervisor bundlea flow + knowledge_base + attached_data (fuente de verdad:
precios.json / info.json) + defaults de objetivo/énfasis. Al crear una auditoría
se elige un Supervisor. RLS scope por tenant; delete = soft-delete.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.db import get_db_with_tenant_context
from app.models import Flow, Project, Supervisor
from app.services.soft_delete import soft_delete_supervisor

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["supervisors"])


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class SupervisorPayload(_CamelModel):
    name: str
    flujo_id: str | None = None
    knowledge_base: str | None = None
    attached_data: dict | None = None
    default_objective: str | None = None
    default_emphasis: list[str] | None = None
    default_free_text: str | None = None


class SupervisorOut(_CamelModel):
    id: str
    name: str
    flujo_id: str | None
    flujo_name: str | None
    knowledge_base: str | None
    attached_data: dict | None
    attached_keys: list[str]
    default_objective: str | None
    default_emphasis: list[str] | None
    default_free_text: str | None
    created_at: str


async def _resolve_flow(
    session: AsyncSession, flujo_public_id: str | None
) -> tuple[uuid.UUID | None, str | None]:
    """flujo public_id → (internal id, name), o (None, None) si no se pasó / 404."""
    if not flujo_public_id:
        return None, None
    row = (
        await session.execute(
            select(Flow.id, Flow.name).where(Flow.public_id == flujo_public_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Flow not found")
    return row.id, row.name


def _to_out(
    sup: Supervisor, flujo_public_id: str | None, flujo_name: str | None
) -> SupervisorOut:
    return SupervisorOut(
        id=sup.public_id,
        name=sup.name,
        flujo_id=flujo_public_id,
        flujo_name=flujo_name,
        knowledge_base=sup.knowledge_base,
        attached_data=sup.attached_data,
        attached_keys=sorted((sup.attached_data or {}).keys()),
        default_objective=sup.default_objective,
        default_emphasis=sup.default_emphasis,
        default_free_text=sup.default_free_text,
        created_at=sup.created_at.isoformat(),
    )


async def _flow_public_and_name(
    session: AsyncSession, flow_id: uuid.UUID | None
) -> tuple[str | None, str | None]:
    if flow_id is None:
        return None, None
    row = (
        await session.execute(
            select(Flow.public_id, Flow.name).where(Flow.id == flow_id)
        )
    ).one_or_none()
    return (row.public_id, row.name) if row else (None, None)


@router.post(
    "/projects/{project_public_id}/supervisors",
    response_model=SupervisorOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_supervisor(
    project_public_id: str,
    payload: SupervisorPayload,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> SupervisorOut:
    project_id = (
        await session.execute(
            select(Project.id).where(Project.public_id == project_public_id)
        )
    ).scalar_one_or_none()
    if project_id is None:
        raise HTTPException(status_code=404, detail="Project not found")

    flow_id, flow_name = await _resolve_flow(session, payload.flujo_id)
    sup_id = uuid.uuid4()
    supervisor = Supervisor(
        id=sup_id,
        public_id=f"sup_{sup_id.hex[:24]}",
        org_id=current_user.org_id,
        project_id=project_id,
        name=payload.name,
        flow_id=flow_id,
        knowledge_base=payload.knowledge_base,
        attached_data=payload.attached_data,
        default_objective=payload.default_objective,
        default_emphasis=payload.default_emphasis,
        default_free_text=payload.default_free_text,
    )
    session.add(supervisor)
    await session.commit()
    logger.info(
        "[SUPERVISORS] Created %s project=%s", supervisor.public_id, project_public_id
    )
    return _to_out(supervisor, payload.flujo_id, flow_name)


@router.get(
    "/projects/{project_public_id}/supervisors",
    response_model=list[SupervisorOut],
)
async def list_supervisors(
    project_public_id: str,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> list[SupervisorOut]:
    project_id = (
        await session.execute(
            select(Project.id).where(Project.public_id == project_public_id)
        )
    ).scalar_one_or_none()
    if project_id is None:
        raise HTTPException(status_code=404, detail="Project not found")

    sups = (
        (
            await session.execute(
                select(Supervisor)
                .where(Supervisor.project_id == project_id)
                .order_by(Supervisor.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    out: list[SupervisorOut] = []
    for sup in sups:
        fpub, fname = await _flow_public_and_name(session, sup.flow_id)
        out.append(_to_out(sup, fpub, fname))
    return out


@router.get("/supervisors/{supervisor_public_id}", response_model=SupervisorOut)
async def get_supervisor(
    supervisor_public_id: str,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> SupervisorOut:
    sup = (
        await session.execute(
            select(Supervisor).where(Supervisor.public_id == supervisor_public_id)
        )
    ).scalar_one_or_none()
    if sup is None:
        raise HTTPException(status_code=404, detail="Supervisor not found")
    fpub, fname = await _flow_public_and_name(session, sup.flow_id)
    return _to_out(sup, fpub, fname)


@router.put("/supervisors/{supervisor_public_id}", response_model=SupervisorOut)
async def update_supervisor(
    supervisor_public_id: str,
    payload: SupervisorPayload,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> SupervisorOut:
    sup = (
        await session.execute(
            select(Supervisor).where(Supervisor.public_id == supervisor_public_id)
        )
    ).scalar_one_or_none()
    if sup is None:
        raise HTTPException(status_code=404, detail="Supervisor not found")

    flow_id, flow_name = await _resolve_flow(session, payload.flujo_id)
    sup.name = payload.name
    sup.flow_id = flow_id
    sup.knowledge_base = payload.knowledge_base
    sup.attached_data = payload.attached_data
    sup.default_objective = payload.default_objective
    sup.default_emphasis = payload.default_emphasis
    sup.default_free_text = payload.default_free_text
    await session.commit()
    return _to_out(sup, payload.flujo_id, flow_name)


@router.delete(
    "/supervisors/{supervisor_public_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_supervisor(
    supervisor_public_id: str,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> None:
    sup_id = (
        await session.execute(
            select(Supervisor.id).where(Supervisor.public_id == supervisor_public_id)
        )
    ).scalar_one_or_none()
    if sup_id is None:
        raise HTTPException(status_code=404, detail="Supervisor not found")
    await soft_delete_supervisor(session, sup_id)
    await session.commit()
