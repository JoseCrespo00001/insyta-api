"""Audit endpoints — create (runs the judge), list, detail with report.

Maps to the frontend `Audit` / `Report` types. POST enqueues `run_audit`;
GET list returns a light report (summary + suggestions); GET detail composes the
full report (conversations + their evaluations + per-message verdicts).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db_with_tenant_context
from app.models import (
    Audit,
    AuditConversation,
    Conversation,
    Evaluation,
    Flow,
    MessageEvaluation,
    Project,
)
from app.services.celery_app import celery_app
from app.services.report_format import eval_to_camel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["audits"])

_SATISFACTION_BUCKETS = {5: "satisfecho", 4: "satisfecho", 3: "neutral"}


class _Camel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class AuditPayload(_Camel):
    name: str | None = None
    objective: str | None = None  # leads | ventas | awareness | soporte | agendar
    provider: str | None = None  # anthropic | deepseek
    flujo_id: str | None = None
    conversation_ids: list[str] = []
    emphasis: list[str] = []
    free_text: str = ""


class AuditCreated(_Camel):
    audit_id: str
    status: str


class Suggestion(_Camel):
    title: str
    detail: str
    impact: str


class AuditSummary(_Camel):
    id: str
    name: str
    flujo_id: str | None
    flujo_name: str | None
    conversation_count: int
    emphasis: list[str]
    free_text: str
    created_at: str
    status: str


async def _resolve_project(
    session: AsyncSession, public_id: str
) -> tuple[uuid.UUID, uuid.UUID]:
    row = (
        await session.execute(
            select(Project.id, Project.org_id).where(Project.public_id == public_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return row.id, row.org_id


@router.post(
    "/projects/{project_public_id}/audits",
    response_model=AuditCreated,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_audit(
    project_public_id: str,
    payload: AuditPayload,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> AuditCreated:
    project_id, org_id = await _resolve_project(session, project_public_id)

    flow_id = None
    flow_name = None
    if payload.flujo_id:
        frow = (
            await session.execute(
                select(Flow.id, Flow.name).where(Flow.public_id == payload.flujo_id)
            )
        ).one_or_none()
        if frow is not None:
            flow_id, flow_name = frow.id, frow.name

    # Resolve conversation public_ids -> internal ids (scoped to the project).
    conv_rows = (
        await session.execute(
            select(Conversation.id, Conversation.public_id).where(
                Conversation.project_id == project_id,
                Conversation.public_id.in_(payload.conversation_ids or []),
            )
        )
    ).all()
    conv_ids = [r.id for r in conv_rows]
    if not conv_ids:
        raise HTTPException(
            status_code=400, detail="No valid conversations selected for audit"
        )

    audit_id = uuid.uuid4()
    public_id = f"aud_{audit_id.hex[:24]}"
    audit = Audit(
        id=audit_id,
        public_id=public_id,
        project_id=project_id,
        org_id=org_id,
        flow_id=flow_id,
        name=(payload.name or "").strip() or f"Auditoría — {flow_name or 'flujo'}",
        objective=(payload.objective or None),
        provider=(payload.provider or "anthropic"),
        emphasis=payload.emphasis,
        free_text=payload.free_text,
        status="running",
        conversation_count=len(conv_ids),
    )
    session.add(audit)
    await session.flush()

    session.add_all(
        [
            AuditConversation(
                id=uuid.uuid4(),
                audit_id=audit_id,
                conversation_id=cid,
                project_id=project_id,
                org_id=org_id,
            )
            for cid in conv_ids
        ]
    )
    await session.flush()

    celery_app.send_task(
        "app.workers.audit.run_audit", args=[str(audit_id), str(org_id)]
    )
    logger.info(
        "[AUDITS] Created %s project=%s convs=%d",
        public_id,
        project_public_id,
        len(conv_ids),
    )
    return AuditCreated(audit_id=public_id, status="running")


@router.get(
    "/projects/{project_public_id}/audits",
    response_model=list[dict],
)
async def list_audits(
    project_public_id: str,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> list[dict]:
    project_id, _ = await _resolve_project(session, project_public_id)
    rows = (
        await session.execute(
            select(Audit, Flow.public_id, Flow.name)
            .outerjoin(Flow, Flow.id == Audit.flow_id)
            .where(Audit.project_id == project_id)
            .order_by(Audit.created_at.desc())
        )
    ).all()
    out = []
    for audit, flow_public_id, flow_name in rows:
        summary = audit.report_summary or {}
        out.append(
            {
                "id": audit.public_id,
                "name": audit.name,
                "objective": audit.objective,
                "flujoId": flow_public_id,
                "flujoName": flow_name,
                "conversationCount": audit.conversation_count,
                "emphasis": audit.emphasis or [],
                "freeText": audit.free_text or "",
                "createdAt": audit.created_at.isoformat(),
                "status": audit.status,
                "report": {
                    "total": summary.get("total", 0),
                    "satisfaction": summary.get("satisfaction", {}),
                    "failing": [],
                    "conversations": [],
                    "suggestions": audit.suggestions or [],
                },
            }
        )
    return out


@router.get("/audits/{audit_public_id}", response_model=dict)
async def get_audit(
    audit_public_id: str,
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> dict:
    arow = (
        await session.execute(
            select(Audit, Flow.public_id, Flow.name)
            .outerjoin(Flow, Flow.id == Audit.flow_id)
            .where(Audit.public_id == audit_public_id)
        )
    ).one_or_none()
    if arow is None:
        raise HTTPException(status_code=404, detail="Audit not found")
    audit, flow_public_id, flow_name = arow

    # Conversations in this audit + their evaluations.
    conv_rows = (
        await session.execute(
            select(Conversation, Evaluation)
            .join(
                AuditConversation,
                AuditConversation.conversation_id == Conversation.id,
            )
            .outerjoin(Evaluation, Evaluation.conversation_id == Conversation.id)
            .where(AuditConversation.audit_id == audit.id)
        )
    ).all()

    # Per-message verdicts for this audit, grouped by conversation.
    me_rows = (
        (
            await session.execute(
                select(MessageEvaluation).where(MessageEvaluation.audit_id == audit.id)
            )
        )
        .scalars()
        .all()
    )
    me_by_conv: dict[uuid.UUID, list] = {}
    for me in me_rows:
        me_by_conv.setdefault(me.conversation_id, []).append(
            {
                "label": me.label,
                "issueType": me.issue_type,
                "issueSubtype": me.issue_subtype,
                "severity": me.severity,
                "note": me.note,
            }
        )

    conversations = []
    failing = []
    buckets = {"satisfecho": 0, "neutral": 0, "insatisfecho": 0}
    scores = []
    for conv, ev in conv_rows:
        sat_bucket = None
        if ev is not None:
            if ev.score is not None:
                scores.append(ev.score)
            sat_bucket = _SATISFACTION_BUCKETS.get(ev.satisfaction or 0, "insatisfecho")
            buckets[sat_bucket] += 1
        item = {
            "id": conv.public_id,
            "externalId": conv.external_id,
            "contactName": conv.contact_name,
            "preview": conv.preview,
            "messageCount": conv.message_count,
            "score": ev.score if ev else None,
            "satisfaction": sat_bucket,
            "resolved": ev.resolution if ev else None,
            "messageEvaluations": me_by_conv.get(conv.id, []),
            # Campos que el contrato Conversation del front espera (ReportView lee
            # evaluation.*; el workspace lee messages/uploadGroupId al abrir una
            # fallida — el transcript se hidrata aparte vía /conversations/{id}).
            "uploadGroupId": str(conv.upload_id) if conv.upload_id else "",
            "userMessages": 0,
            "botMessages": 0,
            "messages": [],
            "selected": False,
            "pinned": False,
            "evaluation": eval_to_camel(ev),
        }
        conversations.append(item)
        if ev is not None and ev.resolution is False:
            failing.append(item)

    return {
        "id": audit.public_id,
        "name": audit.name,
        "objective": audit.objective,
        "flujoId": flow_public_id,
        "flujoName": flow_name,
        "conversationCount": audit.conversation_count,
        "emphasis": audit.emphasis or [],
        "freeText": audit.free_text or "",
        "createdAt": audit.created_at.isoformat(),
        "status": audit.status,
        "report": {
            "total": len(conversations),
            "satisfaction": buckets,
            "avgScore": round(sum(scores) / len(scores)) if scores else None,
            "failing": failing,
            "conversations": conversations,
            "suggestions": audit.suggestions or [],
        },
    }
