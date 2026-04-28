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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.db import get_db_with_tenant_context
from app.models import Project, Upload
from app.services.celery_app import celery_app
from app.services.quotas import FREE_PLAN_MONTHLY_CAP, upload_would_exceed_quota
from app.services.uploads_storage import write_upload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["uploads"])

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB


class UploadCreatedResponse(BaseModel):
    upload_id: str
    status: str = Field(default="pending")


class UploadStatusResponse(BaseModel):
    upload_id: str
    project_public_id: str
    status: str
    rows_total: int | None
    rows_processed: int | None
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
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are accepted")

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

    estimated_rows = max(content.count(b"\n"), 1)
    if await upload_would_exceed_quota(
        session, org_id=current_user.org_id, new_rows=estimated_rows
    ):
        logger.info(
            "[UPLOADS] Quota exceeded org_id=%s estimated=%d cap=%d",
            current_user.org_id,
            estimated_rows,
            FREE_PLAN_MONTHLY_CAP,
        )
        raise HTTPException(
            status_code=402,
            detail=(
                f"Free plan monthly cap of {FREE_PLAN_MONTHLY_CAP} conversations "
                "would be exceeded by this upload"
            ),
        )

    upload_id = uuid.uuid4()
    public_id = f"upl_{upload_id.hex[:24]}"
    storage_path = write_upload(upload_id, content)

    upload = Upload(
        id=upload_id,
        public_id=public_id,
        org_id=current_user.org_id,
        project_id=project_id,
        filename=file.filename,
        storage_path=storage_path,
        size_bytes=len(content),
        status="pending",
    )
    session.add(upload)
    await session.commit()

    celery_app.send_task(
        "app.workers.processor.process_upload",
        args=[str(upload_id)],
    )
    logger.info(
        "[UPLOADS] Accepted public_id=%s project=%s size=%d enqueued",
        public_id,
        project_public_id,
        len(content),
    )

    return UploadCreatedResponse(upload_id=public_id, status="pending")


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
    return UploadStatusResponse(
        upload_id=upload.public_id,
        project_public_id=project_public_id,
        status=upload.status,
        rows_total=upload.rows_total,
        rows_processed=upload.rows_processed,
        error_message=upload.error_message,
    )
