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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.db import get_db_with_tenant_context
from app.models import Flow, FlowVersion, Organization, Project

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


async def _snapshot_version(
    session: AsyncSession,
    flow: Flow,
    *,
    label: str,
    source: str,
) -> FlowVersion:
    """Guarda un snapshot del estado ACTUAL de `flow` como una versión nueva.

    Numeración monotónica por flujo; la versión más alta es siempre la activa
    (un restore crea una versión nueva en vez de mutar el historial).
    """
    last = (
        await session.execute(
            select(func.max(FlowVersion.version_number)).where(
                FlowVersion.flow_id == flow.id
            )
        )
    ).scalar_one_or_none()
    version_number = (last or 0) + 1
    snapshot = FlowVersion(
        public_id=f"flv_{uuid.uuid4().hex[:24]}",
        flow_id=flow.id,
        project_id=flow.project_id,
        org_id=flow.org_id,
        version_number=version_number,
        label=label or f"Versión {version_number}",
        source=source,
        flow_json=flow.flow_json,
        size_bytes=flow.size_bytes,
        agent_count=flow.agent_count,
    )
    session.add(snapshot)
    await session.flush()
    return snapshot


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
    await _snapshot_version(session, flow, label="Versión inicial", source="initial")
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
    # Si cambia flow_json, se guarda una versión en el historial con este nombre.
    version_label: str | None = None
    version_source: str | None = None  # improvement | manual | upload

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
    json_changed = False
    if payload.flow_json is not None:
        metadata = _extract_metadata(payload.flow_json)
        flow.flow_json = payload.flow_json
        flow.flow_metadata = metadata
        flow.size_bytes = len(
            json.dumps(payload.flow_json, ensure_ascii=False).encode("utf-8")
        )
        flow.agent_count = metadata["agentCount"]
        json_changed = True
    await session.flush()
    if json_changed:
        # Cada cambio del JSON queda como versión nueva en el historial.
        source = (
            payload.version_source
            if payload.version_source
            in (
                "improvement",
                "manual",
                "upload",
            )
            else "manual"
        )
        await _snapshot_version(
            session,
            flow,
            label=payload.version_label or "Cambio manual",
            source=source,
        )
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


# ── Historial de versiones del flujo ────────────────────────────────────────


class FlowVersionSummary(_CamelModel):
    id: str
    version_number: int
    label: str
    source: str
    size_bytes: int
    agent_count: int
    created_at: str
    is_current: bool = False


class FlowVersionDetail(FlowVersionSummary):
    json_: str = Field(serialization_alias="json")


async def _load_flow(session: AsyncSession, flow_public_id: str) -> Flow:
    flow = (
        await session.execute(select(Flow).where(Flow.public_id == flow_public_id))
    ).scalar_one_or_none()
    if flow is None:
        raise HTTPException(status_code=404, detail="Flow not found")
    return flow


@router.get("/flows/{flow_public_id}/versions", response_model=list[FlowVersionSummary])
async def list_flow_versions(
    flow_public_id: str,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> list[FlowVersionSummary]:
    """Historial de versiones del flujo (más reciente primero). La versión con
    el número más alto es la activa."""
    flow = await _load_flow(session, flow_public_id)
    rows = (
        (
            await session.execute(
                select(FlowVersion)
                .where(FlowVersion.flow_id == flow.id)
                .order_by(FlowVersion.version_number.desc())
            )
        )
        .scalars()
        .all()
    )
    # Backfill: flujos creados antes del historial no tienen versiones. La
    # primera vez que se abre el historial, sembramos su estado actual como v1.
    if not rows:
        await _snapshot_version(
            session, flow, label="Versión inicial", source="initial"
        )
        rows = (
            (
                await session.execute(
                    select(FlowVersion)
                    .where(FlowVersion.flow_id == flow.id)
                    .order_by(FlowVersion.version_number.desc())
                )
            )
            .scalars()
            .all()
        )
    current = rows[0].version_number if rows else 0
    return [
        FlowVersionSummary(
            id=v.public_id,
            version_number=v.version_number,
            label=v.label,
            source=v.source,
            size_bytes=v.size_bytes,
            agent_count=v.agent_count,
            created_at=v.created_at.isoformat(),
            is_current=v.version_number == current,
        )
        for v in rows
    ]


@router.get(
    "/flows/{flow_public_id}/versions/{version_public_id}",
    response_model=FlowVersionDetail,
)
async def get_flow_version(
    flow_public_id: str,
    version_public_id: str,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> FlowVersionDetail:
    flow = await _load_flow(session, flow_public_id)
    v = (
        await session.execute(
            select(FlowVersion).where(
                FlowVersion.public_id == version_public_id,
                FlowVersion.flow_id == flow.id,
            )
        )
    ).scalar_one_or_none()
    if v is None:
        raise HTTPException(status_code=404, detail="Version not found")
    current = (
        await session.execute(
            select(func.max(FlowVersion.version_number)).where(
                FlowVersion.flow_id == flow.id
            )
        )
    ).scalar_one_or_none() or 0
    return FlowVersionDetail(
        id=v.public_id,
        version_number=v.version_number,
        label=v.label,
        source=v.source,
        size_bytes=v.size_bytes,
        agent_count=v.agent_count,
        created_at=v.created_at.isoformat(),
        is_current=v.version_number == current,
        json_=json.dumps(v.flow_json, ensure_ascii=False, indent=2),
    )


class FlowVersionRename(BaseModel):
    label: str


@router.patch(
    "/flows/{flow_public_id}/versions/{version_public_id}",
    response_model=FlowVersionSummary,
)
async def rename_flow_version(
    flow_public_id: str,
    version_public_id: str,
    payload: FlowVersionRename,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> FlowVersionSummary:
    flow = await _load_flow(session, flow_public_id)
    v = (
        await session.execute(
            select(FlowVersion).where(
                FlowVersion.public_id == version_public_id,
                FlowVersion.flow_id == flow.id,
            )
        )
    ).scalar_one_or_none()
    if v is None:
        raise HTTPException(status_code=404, detail="Version not found")
    label = payload.label.strip()
    if label:
        v.label = label[:200]
    await session.flush()
    return FlowVersionSummary(
        id=v.public_id,
        version_number=v.version_number,
        label=v.label,
        source=v.source,
        size_bytes=v.size_bytes,
        agent_count=v.agent_count,
        created_at=v.created_at.isoformat(),
    )


@router.post(
    "/flows/{flow_public_id}/versions/{version_public_id}/restore",
    response_model=FlowSummary,
)
async def restore_flow_version(
    flow_public_id: str,
    version_public_id: str,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> FlowSummary:
    """Vuelve el flujo a una versión pasada. No borra historial: copia ese JSON
    al flujo y lo guarda como una versión nueva (la activa)."""
    flow = await _load_flow(session, flow_public_id)
    v = (
        await session.execute(
            select(FlowVersion).where(
                FlowVersion.public_id == version_public_id,
                FlowVersion.flow_id == flow.id,
            )
        )
    ).scalar_one_or_none()
    if v is None:
        raise HTTPException(status_code=404, detail="Version not found")

    metadata = _extract_metadata(v.flow_json)
    flow.flow_json = v.flow_json
    flow.flow_metadata = metadata
    flow.size_bytes = v.size_bytes
    flow.agent_count = v.agent_count
    await session.flush()
    await _snapshot_version(
        session,
        flow,
        label=f"Restaurado de v{v.version_number}",
        source="restore",
    )
    return _to_summary(flow)


class FlowAuditRequest(BaseModel):
    mode: str = "standard"  # "standard" | "deep" (Plus)


@router.post("/flows/{flow_public_id}/audit", response_model=dict)
async def audit_flow_endpoint(
    flow_public_id: str,
    payload: FlowAuditRequest,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> dict:
    """Audita el flujo en sí (sin conversaciones) con un LLM experto en Langflow.

    Devuelve {completeness, summary, suggestions[]}. Usa la API key del tenant.
    """
    flow = (
        await session.execute(select(Flow).where(Flow.public_id == flow_public_id))
    ).scalar_one_or_none()
    if flow is None:
        raise HTTPException(status_code=404, detail="Flow not found")

    # Cargar y aplicar la API key del tenant (cifrada).
    org = await session.get(Organization, current_user.org_id)
    enc = org.anthropic_api_key_encrypted if org else None
    if enc:
        from app.llm.credentials import set_llm_keys
        from app.services.secret_crypto import decrypt_secret

        decrypted = decrypt_secret(enc)
        if decrypted:
            set_llm_keys(anthropic=decrypted)

    from app.llm.audit_judge import FatalLLMError
    from app.llm.flow_audit import audit_flow

    mode = "deep" if payload.mode == "deep" else "standard"
    try:
        return await audit_flow(flow.flow_json or {}, mode=mode)
    except FatalLLMError as exc:
        raise HTTPException(
            status_code=400,
            detail="Falta la API key de Anthropic. Cargala en Perfil → Extensiones.",
        ) from exc
