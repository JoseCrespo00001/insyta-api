"""Project CRUD.

POST /api/v1/projects creates a project under the caller's org. RLS enforces
org isolation; uniqueness is `(org_id, slug)`. The slug is derived from the name
when not provided (the dashboard's new-project dialog only sends a name).
"""

from __future__ import annotations

import logging
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.db import get_db_with_tenant_context
from app.models import Agent, Conversation, Evaluation, Project

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["projects"])


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or f"project-{uuid.uuid4().hex[:8]}"


class ProjectCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {"name": "Bot Ventas Q1", "description": "Sales WhatsApp bot"}
        }
    )

    name: str = Field(..., min_length=1, max_length=200)
    slug: str | None = Field(
        default=None, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$"
    )
    description: str | None = Field(default=None, max_length=2000)


class ProjectCreateResponse(BaseModel):
    public_id: str = Field(serialization_alias="publicId")
    name: str
    slug: str

    model_config = ConfigDict(populate_by_name=True)


class ProjectListItem(BaseModel):
    public_id: str = Field(serialization_alias="publicId")
    name: str
    agent_count: int = Field(serialization_alias="agentCount")
    conversation_count: int = Field(serialization_alias="conversationCount")
    score: int | None
    updated_at: str = Field(serialization_alias="updatedAt")
    company_context: str | None = Field(
        default=None, serialization_alias="companyContext"
    )

    model_config = ConfigDict(populate_by_name=True)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    company_context: str | None = Field(default=None, max_length=8000)

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


@router.get("/projects", response_model=list[ProjectListItem])
async def list_projects(
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> list[ProjectListItem]:
    projects = (
        (await session.execute(select(Project).order_by(Project.created_at.desc())))
        .scalars()
        .all()
    )
    out: list[ProjectListItem] = []
    for p in projects:
        agent_count = (
            await session.execute(
                select(func.count(Agent.id)).where(Agent.project_id == p.id)
            )
        ).scalar_one()
        conv_count = (
            await session.execute(
                select(func.count(Conversation.id)).where(
                    Conversation.project_id == p.id
                )
            )
        ).scalar_one()
        avg = (
            await session.execute(
                select(func.avg(Evaluation.score)).where(
                    Evaluation.project_id == p.id, Evaluation.score.isnot(None)
                )
            )
        ).scalar_one()
        out.append(
            ProjectListItem(
                public_id=p.public_id,
                name=p.name,
                agent_count=int(agent_count or 0),
                conversation_count=int(conv_count or 0),
                score=round(avg) if avg is not None else None,
                updated_at=p.updated_at.isoformat(),
                company_context=p.company_context,
            )
        )
    return out


@router.patch("/projects/{project_public_id}", response_model=ProjectListItem)
async def update_project(
    project_public_id: str,
    payload: ProjectUpdate,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> ProjectListItem:
    project = (
        await session.execute(
            select(Project).where(Project.public_id == project_public_id)
        )
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    data = payload.model_dump(exclude_unset=True)
    if "name" in data and data["name"]:
        project.name = data["name"]
    if "description" in data:
        project.description = data["description"]
    if "company_context" in data:
        project.company_context = data["company_context"]
    await session.flush()
    return ProjectListItem(
        public_id=project.public_id,
        name=project.name,
        agent_count=0,
        conversation_count=0,
        score=None,
        updated_at=project.updated_at.isoformat(),
        company_context=project.company_context,
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
    slug = payload.slug or _slugify(payload.name)
    public_id = f"proj_{uuid.uuid4().hex[:24]}"

    project = Project(
        public_id=public_id,
        org_id=current_user.org_id,
        slug=slug,
        name=payload.name,
        description=payload.description,
    )
    session.add(project)
    try:
        await session.flush()
    except IntegrityError as exc:
        logger.info(
            "[PROJECTS] Duplicate slug org_id=%s slug=%s",
            current_user.org_id,
            slug,
        )
        raise HTTPException(
            status_code=409,
            detail=f"Project slug '{slug}' already exists in this organization",
        ) from exc

    logger.info(
        "[PROJECTS] Created public_id=%s org_id=%s slug=%s",
        public_id,
        current_user.org_id,
        slug,
    )
    return ProjectCreateResponse(public_id=public_id, name=payload.name, slug=slug)
