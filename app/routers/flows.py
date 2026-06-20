"""Flow endpoints — upload / list / get the agent's Langflow "brain".

Maps to the frontend `Flujo` type. Responses are camelCase (alias) so the
dashboard consumes them directly. RLS scopes everything by tenant.
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db_with_tenant_context
from app.models import Flow, Project

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["flows"])


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class FlowSummary(_CamelModel):
    id: str
    name: str
    version: str
    size_bytes: int
    agent_count: int
    created_at: str


class FlowDetail(FlowSummary):
    # serialized flow_json (pretty-printed). Explicit alias so it ships as "json".
    json_: str = Field(serialization_alias="json")


class FlowCreate(BaseModel):
    name: str
    version: str | None = None
    flow_json: dict = {}

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


def _extract_metadata(flow_json: dict) -> dict:
    """Pull a light metadata blob from a Langflow export (nodes/edges)."""
    data = flow_json.get("data", flow_json)
    nodes = data.get("nodes", []) if isinstance(data, dict) else []
    edges = data.get("edges", []) if isinstance(data, dict) else []
    node_types: list[str] = []
    agent_count = 0
    for n in nodes:
        ndata = n.get("data", {}) or {}
        ntype = ndata.get("type") or n.get("type")
        if ntype and ntype != "genericNode":
            node_types.append(ntype)
        # Count agents robustly across Langflow variants: data.type, node id
        # prefix, or the component display name.
        display = (ndata.get("node", {}) or {}).get("display_name", "") or ""
        nid = str(n.get("id") or ndata.get("id") or "")
        if ntype == "Agent" or nid.startswith("Agent") or display == "Agent":
            agent_count += 1
    return {
        "nodesCount": len(nodes),
        "edgesCount": len(edges),
        "nodeTypes": sorted(set(node_types)),
        "agentCount": agent_count,
        "name": flow_json.get("name"),
    }


async def _resolve_project(
    session: AsyncSession, public_id: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """Return (project_id, org_id) for a project public_id, or 404."""
    result = await session.execute(
        select(Project.id, Project.org_id).where(Project.public_id == public_id)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return row.id, row.org_id


def _to_summary(flow: Flow) -> FlowSummary:
    return FlowSummary(
        id=flow.public_id,
        name=flow.name,
        version=flow.version,
        size_bytes=flow.size_bytes,
        agent_count=flow.agent_count,
        created_at=flow.created_at.isoformat(),
    )


@router.get("/flows", response_model=list[FlowSummary])
async def list_all_flows(
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> list[FlowSummary]:
    """Todos los flujos visibles para el tenant (RLS filtra por org/proyectos).

    Lo usa la vista global de Mejoras para elegir un flujo sin entrar al proyecto.
    """
    result = await session.execute(select(Flow).order_by(Flow.created_at.desc()))
    return [_to_summary(f) for f in result.scalars()]


@router.get("/projects/{project_public_id}/flows", response_model=list[FlowSummary])
async def list_flows(
    project_public_id: str,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> list[FlowSummary]:
    project_id, _ = await _resolve_project(session, project_public_id)
    result = await session.execute(
        select(Flow)
        .where(Flow.project_id == project_id)
        .order_by(Flow.created_at.desc())
    )
    return [_to_summary(f) for f in result.scalars()]


@router.get("/flows/{flow_public_id}", response_model=FlowDetail)
async def get_flow(
    flow_public_id: str,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> FlowDetail:
    result = await session.execute(select(Flow).where(Flow.public_id == flow_public_id))
    flow = result.scalar_one_or_none()
    if flow is None:
        raise HTTPException(status_code=404, detail="Flow not found")
    return FlowDetail(
        id=flow.public_id,
        name=flow.name,
        version=flow.version,
        size_bytes=flow.size_bytes,
        agent_count=flow.agent_count,
        created_at=flow.created_at.isoformat(),
        json_=json.dumps(flow.flow_json, ensure_ascii=False, indent=2),
    )


@router.post(
    "/projects/{project_public_id}/flows",
    response_model=FlowSummary,
    status_code=status.HTTP_201_CREATED,
)
async def create_flow(
    project_public_id: str,
    payload: FlowCreate,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> FlowSummary:
    project_id, org_id = await _resolve_project(session, project_public_id)

    flow_json = payload.flow_json or {}
    metadata = _extract_metadata(flow_json)
    size_bytes = len(json.dumps(flow_json, ensure_ascii=False).encode("utf-8"))
    public_id = f"flj_{uuid.uuid4().hex[:24]}"

    flow = Flow(
        public_id=public_id,
        project_id=project_id,
        org_id=org_id,
        name=payload.name,
        version=payload.version or "1.0",
        flow_json=flow_json,
        flow_metadata=metadata,
        size_bytes=size_bytes,
        agent_count=metadata["agentCount"],
        is_active=True,
    )
    session.add(flow)
    await session.flush()
    logger.info(
        "[FLOWS] Created public_id=%s project=%s agents=%d size=%d",
        public_id,
        project_public_id,
        metadata["agentCount"],
        size_bytes,
    )
    return _to_summary(flow)


class FlowUpdate(BaseModel):
    name: str | None = None
    version: str | None = None
    flow_json: dict | None = None

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


@router.put("/flows/{flow_public_id}", response_model=FlowSummary)
async def update_flow(
    flow_public_id: str,
    payload: FlowUpdate,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> FlowSummary:
    flow = (
        await session.execute(select(Flow).where(Flow.public_id == flow_public_id))
    ).scalar_one_or_none()
    if flow is None:
        raise HTTPException(status_code=404, detail="Flow not found")
    if payload.name is not None:
        flow.name = payload.name
    if payload.version is not None:
        flow.version = payload.version
    if payload.flow_json is not None:
        metadata = _extract_metadata(payload.flow_json)
        flow.flow_json = payload.flow_json
        flow.flow_metadata = metadata
        flow.size_bytes = len(
            json.dumps(payload.flow_json, ensure_ascii=False).encode("utf-8")
        )
        flow.agent_count = metadata["agentCount"]
    await session.flush()
    return _to_summary(flow)


@router.delete("/flows/{flow_public_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_flow(
    flow_public_id: str,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> None:
    flow = (
        await session.execute(select(Flow).where(Flow.public_id == flow_public_id))
    ).scalar_one_or_none()
    if flow is None:
        raise HTTPException(status_code=404, detail="Flow not found")
    await session.delete(flow)
    await session.flush()
