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

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db_with_tenant_context
from app.models import (
    Conversation,
    Evaluation,
    Message,
    MessageEvaluation,
    Project,
    Upload,
)
from app.services.report_format import eval_to_camel
from app.services.soft_delete import soft_delete_conversations

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["conversations"])

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


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
    content: str
    content_anonymized: str | None
    timestamp: str
    # Veredicto del judge para este mensaje (última auditoría), si existe.
    label: str | None = None  # ok | warning | error
    issue_type: str | None = None  # alucinacion | error_politica | ...
    severity: str | None = None
    note: str | None = None


class ConversationDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    public_id: str
    external_id: str
    platform: str
    status: str
    started_at: str | None
    messages: list[MessageOut]
    # ConversationEvaluation (camelCase) que consume ConversationReport del front.
    evaluation: dict | None


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
            Upload.public_id.label("upload_public_id"),
            Evaluation.score,
            Evaluation.satisfaction,
            Evaluation.resolution,
        )
        .outerjoin(Evaluation, Evaluation.conversation_id == Conversation.id)
        .outerjoin(Upload, Upload.id == Conversation.upload_id)
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
            upload_group_id=r.upload_public_id,
            satisfaction=_sat(r.satisfaction),
            resolved=r.resolution,
        )
        for r in page_rows
    ]
    next_cursor = _encode_cursor(page_rows[-1].id) if has_more and page_rows else None

    return ConversationsPage(items=items, next_cursor=next_cursor)


class GlobalGroupConv(BaseModel):
    public_id: str
    external_id: str
    contact_name: str | None = None
    preview: str | None = None
    message_count: int = 0
    score: int | None = None
    satisfaction: str | None = None
    resolved: bool | None = None


class GlobalUploadGroup(BaseModel):
    id: str
    project_name: str
    filename: str
    loaded_at: str
    conversations: list[GlobalGroupConv]


def _sat_bucket(v: int | None) -> str | None:
    if v is None:
        return None
    return "satisfecho" if v >= 4 else "neutral" if v == 3 else "insatisfecho"


@router.get("/conversations", response_model=list[GlobalUploadGroup])
async def list_all_conversations(
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> list[GlobalUploadGroup]:
    """Conversaciones de todo el tenant agrupadas por CSV (upload) y proyecto.

    Lo usa la vista global /conversations. RLS filtra por org/proyectos visibles.
    """
    upload_rows = (
        await session.execute(
            select(
                Upload.id,
                Upload.public_id,
                Upload.filename,
                Upload.created_at,
                Project.name,
            )
            .join(Project, Project.id == Upload.project_id)
            .order_by(Upload.created_at.desc())
        )
    ).all()
    if not upload_rows:
        return []

    upload_ids = [r.id for r in upload_rows]
    conv_rows = (
        await session.execute(
            select(
                Conversation.upload_id,
                Conversation.public_id,
                Conversation.external_id,
                Conversation.contact_name,
                Conversation.preview,
                Conversation.message_count,
                Evaluation.score,
                Evaluation.satisfaction,
                Evaluation.resolution,
            )
            .outerjoin(Evaluation, Evaluation.conversation_id == Conversation.id)
            .where(Conversation.upload_id.in_(upload_ids))
            .order_by(Conversation.id.desc())
        )
    ).all()

    convs_by_upload: dict = {}
    for r in conv_rows:
        convs_by_upload.setdefault(r.upload_id, []).append(
            GlobalGroupConv(
                public_id=r.public_id,
                external_id=r.external_id,
                contact_name=r.contact_name,
                preview=r.preview,
                message_count=r.message_count or 0,
                score=r.score,
                satisfaction=_sat_bucket(r.satisfaction),
                resolved=r.resolution,
            )
        )

    groups: list[GlobalUploadGroup] = []
    for u in upload_rows:
        convs = convs_by_upload.get(u.id, [])
        if not convs:
            continue
        groups.append(
            GlobalUploadGroup(
                id=u.public_id,
                project_name=u.name,
                filename=u.filename or "carga.csv",
                loaded_at=u.created_at.isoformat(),
                conversations=convs,
            )
        )
    return groups


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

    # Veredictos por mensaje (último por mensaje, de la auditoría más reciente).
    me_rows = (
        (
            await session.execute(
                select(MessageEvaluation)
                .where(MessageEvaluation.conversation_id == conv.id)
                .order_by(MessageEvaluation.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    verdict_by_msg: dict = {}
    for me in me_rows:
        verdict_by_msg[me.message_id] = me  # asc → queda el más reciente

    messages_out: list[MessageOut] = []
    for m in messages:
        v = verdict_by_msg.get(m.id)
        messages_out.append(
            MessageOut(
                # Texto real para el dueño (su data, bajo RLS). La versión
                # anonimizada (content_anonymized) es la que va al LLM.
                public_id=m.public_id,
                role=m.role,
                content=m.content,
                content_anonymized=m.content_anonymized,
                timestamp=m.timestamp.isoformat(),
                label=v.label if v else None,
                issue_type=v.issue_type if v else None,
                severity=v.severity if v else None,
                note=v.note if v else None,
            )
        )

    return ConversationDetail(
        public_id=conv.public_id,
        external_id=conv.external_id,
        platform=conv.platform,
        status=conv.status,
        started_at=conv.started_at.isoformat() if conv.started_at else None,
        messages=messages_out,
        evaluation=eval_to_camel(evaluation) if evaluation else None,
    )


@router.delete(
    "/conversations/{conversation_public_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_conversation(
    conversation_public_id: str,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> None:
    """Soft-delete de una conversación: setea is_deleted=True en la conversación
    y sus hijos (mensajes, evaluations, verdicts). No borra filas."""
    conv_id = (
        await session.execute(
            select(Conversation.id).where(
                Conversation.public_id == conversation_public_id
            )
        )
    ).scalar_one_or_none()
    if conv_id is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    await soft_delete_conversations(session, [conv_id])
    await session.commit()
