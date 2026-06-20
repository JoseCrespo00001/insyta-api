"""Improvement endpoints — per-flow improvement proposals + status updates.

Maps to the frontend `FlujoImprovement` type ({title, detail, impact, why,
conversations[]}). Improvements are produced by audits (see workers/audit.py).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db_with_tenant_context
from app.models import (
    Conversation,
    Evaluation,
    Flow,
    Improvement,
    ImprovementConversation,
    Project,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["improvements"])

_ALLOWED_STATUS = {"pending", "approved", "rejected", "applied", "measured"}


class StatusUpdate(BaseModel):
    status: str


async def _improvements_payload(
    session: AsyncSession, improvements: list[Improvement]
) -> list[dict]:
    """Attach the affected conversations to each improvement."""
    if not improvements:
        return []
    imp_ids = [imp.id for imp in improvements]
    # Affected conversations per improvement (with score for context).
    rows = (
        await session.execute(
            select(
                ImprovementConversation.improvement_id,
                Conversation.public_id,
                Conversation.external_id,
                Conversation.contact_name,
                Conversation.preview,
                Evaluation.score,
            )
            .join(
                Conversation,
                Conversation.id == ImprovementConversation.conversation_id,
            )
            .outerjoin(Evaluation, Evaluation.conversation_id == Conversation.id)
            .where(ImprovementConversation.improvement_id.in_(imp_ids))
        )
    ).all()
    convs_by_imp: dict = {}
    for r in rows:
        convs_by_imp.setdefault(r.improvement_id, []).append(
            {
                "id": r.public_id,
                "externalId": r.external_id,
                "contactName": r.contact_name,
                "preview": r.preview,
                "score": r.score,
            }
        )
    return [
        {
            "id": imp.public_id,
            "title": imp.title,
            "detail": imp.detail,
            "impact": imp.impact,
            "why": imp.why,
            "status": imp.status,
            "conversations": convs_by_imp.get(imp.id, []),
        }
        for imp in improvements
    ]


@router.get("/flows/{flow_public_id}/improvements", response_model=list[dict])
async def list_flow_improvements(
    flow_public_id: str,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> list[dict]:
    flow_id = (
        await session.execute(select(Flow.id).where(Flow.public_id == flow_public_id))
    ).scalar_one_or_none()
    if flow_id is None:
        raise HTTPException(status_code=404, detail="Flow not found")
    improvements = (
        (
            await session.execute(
                select(Improvement)
                .where(Improvement.flow_id == flow_id)
                .order_by(Improvement.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return await _improvements_payload(session, list(improvements))


@router.get("/projects/{project_public_id}/improvements", response_model=list[dict])
async def list_project_improvements(
    project_public_id: str,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> list[dict]:
    project_id = (
        await session.execute(
            select(Project.id).where(Project.public_id == project_public_id)
        )
    ).scalar_one_or_none()
    if project_id is None:
        raise HTTPException(status_code=404, detail="Project not found")
    improvements = (
        (
            await session.execute(
                select(Improvement)
                .where(Improvement.project_id == project_id)
                .order_by(Improvement.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return await _improvements_payload(session, list(improvements))


@router.patch("/improvements/{improvement_public_id}", response_model=dict)
async def update_improvement_status(
    improvement_public_id: str,
    payload: StatusUpdate,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> dict:
    if payload.status not in _ALLOWED_STATUS:
        raise HTTPException(status_code=422, detail="Invalid status")
    imp = (
        await session.execute(
            select(Improvement).where(Improvement.public_id == improvement_public_id)
        )
    ).scalar_one_or_none()
    if imp is None:
        raise HTTPException(status_code=404, detail="Improvement not found")
    imp.status = payload.status
    await session.flush()
    return {"id": imp.public_id, "status": imp.status}
