"""Project CRUD.

POST /api/v1/projects creates a project under the caller's org. The webhook
secret is generated server-side, returned RAW once in the response, and stored
encrypted (Fernet) in the database. RLS enforces org isolation; uniqueness is
`(org_id, slug)`.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.db import get_db_with_tenant_context
from app.models import Project
from app.services.webhook_secret import (
    encrypt_webhook_secret,
    generate_raw_webhook_secret,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["projects"])


class ProjectCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Bot Ventas Q1",
                "slug": "bot-ventas-q1",
                "description": "Sales WhatsApp bot",
            }
        }
    )

    name: str = Field(..., min_length=1, max_length=200)
    slug: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str | None = Field(default=None, max_length=2000)


class ProjectCreateResponse(BaseModel):
    public_id: str
    name: str
    slug: str
    webhook_secret: str = Field(
        description="Raw webhook secret. Returned once — store it now; not retrievable later."
    )


@router.post(
    "/projects",
    response_model=ProjectCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_project(
    payload: ProjectCreate,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> ProjectCreateResponse:
    raw_secret = generate_raw_webhook_secret()
    encrypted = encrypt_webhook_secret(raw_secret)
    public_id = f"proj_{uuid.uuid4().hex[:24]}"

    project = Project(
        public_id=public_id,
        org_id=current_user.org_id,
        slug=payload.slug,
        name=payload.name,
        description=payload.description,
        webhook_secret_encrypted=encrypted,
    )
    session.add(project)
    try:
        await session.flush()
    except IntegrityError as exc:
        logger.info(
            "[PROJECTS] Duplicate slug org_id=%s slug=%s",
            current_user.org_id,
            payload.slug,
        )
        raise HTTPException(
            status_code=409,
            detail=f"Project slug '{payload.slug}' already exists in this organization",
        ) from exc

    logger.info(
        "[PROJECTS] Created public_id=%s org_id=%s slug=%s",
        public_id,
        current_user.org_id,
        payload.slug,
    )
    return ProjectCreateResponse(
        public_id=public_id,
        name=payload.name,
        slug=payload.slug,
        webhook_secret=raw_secret,
    )
