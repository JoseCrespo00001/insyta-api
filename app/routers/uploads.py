"""POST /api/v1/uploads/csv (multipart) + GET /api/v1/uploads/{public_id}.

The POST endpoint validates size + extension, persists the file, inserts an
`uploads` row in `pending` status, and enqueues `process_upload(upload_id)` on
Celery. We send by name (`celery_app.send_task("...", args=[...])`) so the
worker module does not need to be importable from the API process.

The GET endpoint is the SDK polling target; it returns the current status,
row counts and any error message.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.db import get_db_with_tenant_context
from app.models import Agent, Conversation, Project, Upload
from app.services.celery_app import celery_app
from app.services.uploads_storage import write_upload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["uploads"])

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB


async def _get_or_create_default_agent(
    session: AsyncSession, *, project_id: uuid.UUID, org_id: uuid.UUID
) -> uuid.UUID:
    """Conversations require an agent. CSV uploads attach to a per-project
    'default' agent, created on first upload."""
    existing = await session.execute(
        select(Agent.id).where(Agent.project_id == project_id, Agent.slug == "default")
    )
    agent_id = existing.scalar_one_or_none()
    if agent_id is not None:
        return agent_id
    agent_id = uuid.uuid4()
    session.add(
        Agent(
            id=agent_id,
            public_id=f"agt_{agent_id.hex[:24]}",
            project_id=project_id,
            org_id=org_id,
            slug="default",
            name="Default agent",
            platform="custom_sdk",
        )
    )
    await session.flush()
    return agent_id


class UploadCreatedResponse(BaseModel):
    upload_id: str
    status: str = Field(default="pending")


class UploadStatusResponse(BaseModel):
    upload_id: str
    project_public_id: str
    status: str
    rows_total: int | None
    rows_processed: int | None
    progress_pct: int
    error_message: str | None


@router.post(
    "/uploads/csv",
    response_model=UploadCreatedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_upload(
    project_public_id: str = Form(...),
    file: UploadFile = File(...),
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> UploadCreatedResponse:
    if not file.filename or not file.filename.lower().endswith((".csv", ".txt")):
        raise HTTPException(
            status_code=400,
            detail="Solo se aceptan archivos .csv o .txt (export de WhatsApp)",
        )

    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit",
        )

    project_result = await session.execute(
        select(Project.id).where(Project.public_id == project_public_id)
    )
    project_id = project_result.scalar_one_or_none()
    if project_id is None:
        raise HTTPException(status_code=404, detail="Project not found")

    agent_id = await _get_or_create_default_agent(
        session, project_id=project_id, org_id=current_user.org_id
    )

    upload_id = uuid.uuid4()
    public_id = f"upl_{upload_id.hex[:24]}"
    storage_path = write_upload(upload_id, content)

    upload = Upload(
        id=upload_id,
        public_id=public_id,
        org_id=current_user.org_id,
        project_id=project_id,
        agent_id=agent_id,
        filename=file.filename,
        storage_path=storage_path,
        size_bytes=len(content),
        status="pending",
    )
    session.add(upload)
    await session.commit()

    celery_app.send_task(
        "app.workers.processor.process_upload",
        args=[str(upload_id), str(current_user.org_id)],
    )
    logger.info(
        "[UPLOADS] Accepted public_id=%s project=%s size=%d enqueued",
        public_id,
        project_public_id,
        len(content),
    )

    return UploadCreatedResponse(upload_id=public_id, status="pending")


class UploadGroupItem(BaseModel):
    id: str
    filename: str
    loaded_at: str = Field(serialization_alias="loadedAt")
    status: str
    conversation_count: int = Field(serialization_alias="conversationCount")
    progress_pct: int = Field(serialization_alias="progressPct")

    model_config = {"populate_by_name": True}


@router.get(
    "/projects/{project_public_id}/uploads",
    response_model=list[UploadGroupItem],
)
async def list_project_uploads(
    project_public_id: str,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> list[UploadGroupItem]:
    proj = await session.execute(
        select(Project.id).where(Project.public_id == project_public_id)
    )
    project_id = proj.scalar_one_or_none()
    if project_id is None:
        raise HTTPException(status_code=404, detail="Project not found")

    from app.models import Conversation

    rows = await session.execute(
        select(
            Upload.public_id,
            Upload.filename,
            Upload.created_at,
            Upload.status,
            Upload.rows_total,
            Upload.rows_processed,
            func.count(Conversation.id).label("conv_count"),
        )
        .outerjoin(Conversation, Conversation.upload_id == Upload.id)
        .where(Upload.project_id == project_id)
        .group_by(Upload.id)
        .order_by(Upload.created_at.desc())
    )
    out: list[UploadGroupItem] = []
    for r in rows:
        total = r.rows_total or 0
        done = r.rows_processed or 0
        pct = (
            100
            if r.status == "completed"
            else (round(done * 100 / total) if total else 0)
        )
        out.append(
            UploadGroupItem(
                id=r.public_id,
                filename=r.filename,
                loaded_at=r.created_at.isoformat(),
                status=r.status,
                conversation_count=int(r.conv_count or 0),
                progress_pct=pct,
            )
        )
    return out


@router.get(
    "/uploads/{upload_public_id}",
    response_model=UploadStatusResponse,
)
async def get_upload(
    upload_public_id: str,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> UploadStatusResponse:
    result = await session.execute(
        select(Upload, Project.public_id)
        .join(Project, Project.id == Upload.project_id)
        .where(Upload.public_id == upload_public_id)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Upload not found")

    upload, project_public_id = row
    total = upload.rows_total or 0
    done = upload.rows_processed or 0
    progress_pct = (
        100
        if upload.status == "completed"
        else (round(done * 100 / total) if total else 0)
    )
    return UploadStatusResponse(
        upload_id=upload.public_id,
        project_public_id=project_public_id,
        status=upload.status,
        rows_total=upload.rows_total,
        rows_processed=upload.rows_processed,
        progress_pct=progress_pct,
        error_message=upload.error_message,
    )


@router.delete(
    "/uploads/{upload_public_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_upload(
    upload_public_id: str,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> None:
    """Borra un CSV completo: sus conversaciones (cascada de mensajes/evals) +
    la fila del upload + el archivo en disco."""
    row = (
        await session.execute(
            select(Upload.id, Upload.storage_path).where(
                Upload.public_id == upload_public_id
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Upload not found")

    await session.execute(delete(Conversation).where(Conversation.upload_id == row.id))
    await session.execute(delete(Upload).where(Upload.id == row.id))
    await session.commit()

    # Mejor esfuerzo: borrar el archivo del storage.
    if row.storage_path:
        import contextlib
        import os

        with contextlib.suppress(OSError):
            os.remove(row.storage_path)
