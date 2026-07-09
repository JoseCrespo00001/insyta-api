"""Clients endpoints — perfil por cliente final del agente (no el usuario logueado).

Agrega lo persistido por (project, external_id): reputación (respetuoso/etiqueta),
recurrencia, temas, sentimiento y a qué hora responde. Para que el dueño conozca a
su cliente y personalice la atención, con export CSV. RLS por project vía tenant ctx.
"""

from __future__ import annotations

import csv
import io
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db_with_tenant_context
from app.models import Project
from app.services.client_profile import (
    get_client_profile,
    list_clients,
    resolve_external_id,
)

router = APIRouter(prefix="/api/v1", tags=["clients"])


async def _resolve_project_id(session: AsyncSession, public_id: str) -> uuid.UUID:
    pid = (
        await session.execute(select(Project.id).where(Project.public_id == public_id))
    ).scalar_one_or_none()
    if pid is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return pid


@router.get("/projects/{project_public_id}/clients", response_model=list[dict])
async def get_clients(
    project_public_id: str,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> list[dict]:
    project_id = await _resolve_project_id(session, project_public_id)
    return await list_clients(session, project_id)


@router.get("/projects/{project_public_id}/clients/profile", response_model=dict)
async def get_client(
    project_public_id: str,
    client: str = Query(..., description="user_key (hash) del cliente"),
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> dict:
    project_id = await _resolve_project_id(session, project_public_id)
    # B5: el cliente se pide por user_key (hash), no por teléfono crudo en la URL.
    external_id = await resolve_external_id(session, project_id, client)
    profile = await get_client_profile(session, project_id, external_id) if external_id else None
    if profile is None:
        raise HTTPException(status_code=404, detail="Client not found")
    return profile


@router.get("/projects/{project_public_id}/clients/export.csv")
async def export_clients_csv(
    project_public_id: str,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> StreamingResponse:
    """CSV de todos los clientes del proyecto (para campañas / CRM)."""
    project_id = await _resolve_project_id(session, project_public_id)
    clients = await list_clients(session, project_id)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "cliente",  # B5: display pseudónimo, nunca teléfono crudo
            "user_key",
            "conversaciones",
            "score_promedio",
            "satisfaccion_promedio",
            "etiqueta",
            "respetuoso",
            "riesgoso",
            "intentos_fraude",
            "ultimo_visto",
        ]
    )
    for c in clients:
        writer.writerow(
            [
                c["display"],
                c["userKey"],
                c["conversations"],
                c["avgScore"] if c["avgScore"] is not None else "",
                c["avgSatisfaction"] if c["avgSatisfaction"] is not None else "",
                c["etiqueta"],
                "si" if c["respectful"] else "no",
                "si" if c["riesgoso"] else "no",
                c["fraudAttempts"],
                c["lastSeen"] or "",
            ]
        )
    buf.seek(0)
    filename = f"clientes_{project_public_id}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
