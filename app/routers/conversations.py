"""Read endpoints for projects, conversations and conversation detail.

All routes use `get_db_with_tenant_context` so PostgreSQL RLS filters rows by
`(org_id, allowed_project_ids)` set from the JWT. Cross-tenant access surfaces
as 404 (not 403): the row simply does not exist for this caller.

Pagination uses an opaque cursor over UUIDv7 — `created_at` is encoded into the
high bits of the id, so ordering by id descending is monotonic without needing
a separate timestamp tiebreaker.
"""

from __future__ import annotations

import base64
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db_with_tenant_context
from app.models import Conversation, Evaluation, Message, Project

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["reads"])

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class ProjectScoreResponse(BaseModel):
    project_public_id: str
    score: int | None = Field(description="0-100 average; null if no evaluations yet")
    evaluation_count: int


class ConversationSummary(BaseModel):
    public_id: str
    external_id: str
    platform: str
    status: str
    started_at: str | None
    score: int | None
    contact_name: str | None = None
    preview: str | None = None
    message_count: int = 0
    upload_group_id: str | None = None
    satisfaction: str | None = None
    resolved: bool | None = None


class ConversationsPage(BaseModel):
    items: list[ConversationSummary]
    next_cursor: str | None


class MessageOut(BaseModel):
    public_id: str
    role: str
    content_anonymized: str | None
    timestamp: str


class EvaluationOut(BaseModel):
    score: int | None
    resolution: bool | None
    satisfaction: int | None
    tone: str | None
    frustration: bool | None
    escalated: bool | None
    efficiency: int | None
    scope_violation: bool | None
    topic: str | None
    summary: str | None
    model_used: str | None
    tokens_used: int | None
    cost_usd: str | None
    phoenix_span_id: str | None
    evaluated_at: str | None


class ConversationDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    public_id: str
    external_id: str
    platform: str
    status: str
    started_at: str | None
    messages: list[MessageOut]
    evaluation: EvaluationOut | None


def _encode_cursor(value: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(value.bytes).decode().rstrip("=")


def _decode_cursor(value: str) -> uuid.UUID:
    padded = value + "=" * (-len(value) % 4)
    try:
        return uuid.UUID(bytes=base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid cursor") from exc


async def _resolve_project_id(session: AsyncSession, public_id: str) -> uuid.UUID:
    result = await session.execute(
        select(Project.id).where(Project.public_id == public_id)
    )
    project_id = result.scalar_one_or_none()
    if project_id is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project_id


@router.get(
    "/projects/{project_public_id}/score",
    response_model=ProjectScoreResponse,
)
async def get_project_score(
    project_public_id: str,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> ProjectScoreResponse:
    project_id = await _resolve_project_id(session, project_public_id)

    result = await session.execute(
        select(
            func.avg(Evaluation.score).label("avg_score"),
            func.count(Evaluation.id).label("count"),
        ).where(Evaluation.project_id == project_id, Evaluation.score.isnot(None))
    )
    row = result.one()
    avg = int(round(row.avg_score)) if row.avg_score is not None else None
    return ProjectScoreResponse(
        project_public_id=project_public_id,
        score=avg,
        evaluation_count=int(row.count or 0),
    )


@router.get(
    "/projects/{project_public_id}/conversations",
    response_model=ConversationsPage,
)
async def list_project_conversations(
    project_public_id: str,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> ConversationsPage:
    project_id = await _resolve_project_id(session, project_public_id)

    stmt = (
        select(
            Conversation.id,
            Conversation.public_id,
            Conversation.external_id,
            Conversation.platform,
            Conversation.status,
            Conversation.started_at,
            Conversation.contact_name,
            Conversation.preview,
            Conversation.message_count,
            Conversation.upload_id,
            Evaluation.score,
            Evaluation.satisfaction,
            Evaluation.resolution,
        )
        .outerjoin(Evaluation, Evaluation.conversation_id == Conversation.id)
        .where(Conversation.project_id == project_id)
        .order_by(Conversation.id.desc())
        .limit(limit + 1)
    )
    if cursor is not None:
        cursor_uuid = _decode_cursor(cursor)
        stmt = stmt.where(Conversation.id < cursor_uuid)

    rows = (await session.execute(stmt)).all()
    has_more = len(rows) > limit
    page_rows = rows[:limit]

    def _sat(v: int | None) -> str | None:
        if v is None:
            return None
        return "satisfecho" if v >= 4 else "neutral" if v == 3 else "insatisfecho"

    items = [
        ConversationSummary(
            public_id=r.public_id,
            external_id=r.external_id,
            platform=r.platform,
            status=r.status,
            started_at=r.started_at.isoformat() if r.started_at else None,
            score=r.score,
            contact_name=r.contact_name,
            preview=r.preview,
            message_count=r.message_count or 0,
            upload_group_id=str(r.upload_id) if r.upload_id else None,
            satisfaction=_sat(r.satisfaction),
            resolved=r.resolution,
        )
        for r in page_rows
    ]
    next_cursor = _encode_cursor(page_rows[-1].id) if has_more and page_rows else None

    return ConversationsPage(items=items, next_cursor=next_cursor)


@router.get(
    "/conversations/{conversation_public_id}",
    response_model=ConversationDetail,
)
async def get_conversation_detail(
    conversation_public_id: str,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> ConversationDetail:
    conv_result = await session.execute(
        select(Conversation).where(Conversation.public_id == conversation_public_id)
    )
    conv = conv_result.scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    messages_result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.timestamp.asc())
    )
    messages = messages_result.scalars().all()

    eval_result = await session.execute(
        select(Evaluation).where(Evaluation.conversation_id == conv.id)
    )
    evaluation = eval_result.scalar_one_or_none()

    return ConversationDetail(
        public_id=conv.public_id,
        external_id=conv.external_id,
        platform=conv.platform,
        status=conv.status,
        started_at=conv.started_at.isoformat() if conv.started_at else None,
        messages=[
            MessageOut(
                public_id=m.public_id,
                role=m.role,
                content_anonymized=m.content_anonymized,
                timestamp=m.timestamp.isoformat(),
            )
            for m in messages
        ],
        evaluation=(
            EvaluationOut(
                score=evaluation.score,
                resolution=evaluation.resolution,
                satisfaction=evaluation.satisfaction,
                tone=evaluation.tone,
                frustration=evaluation.frustration,
                escalated=evaluation.escalated,
                efficiency=evaluation.efficiency,
                scope_violation=evaluation.scope_violation,
                topic=evaluation.topic,
                summary=evaluation.summary,
                model_used=evaluation.model_used,
                tokens_used=evaluation.tokens_used,
                cost_usd=(
                    str(evaluation.cost_usd)
                    if evaluation.cost_usd is not None
                    else None
                ),
                phoenix_span_id=evaluation.phoenix_span_id,
                evaluated_at=(
                    evaluation.evaluated_at.isoformat()
                    if evaluation.evaluated_at
                    else None
                ),
            )
            if evaluation
            else None
        ),
    )
