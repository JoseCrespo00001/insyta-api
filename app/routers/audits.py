"""Audit endpoints — create (runs the judge), list, detail with report.

Maps to the frontend `Audit` / `Report` types. POST enqueues `run_audit`;
GET list returns a light report (summary + suggestions); GET detail composes the
full report (conversations + their evaluations + per-message verdicts).
"""

from __future__ import annotations

import csv
import io
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
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
    Supervisor,
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
    supervisor_id: str | None = None  # cerebro reusable; aporta flow + defaults
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

    # Supervisor (opcional): aporta flow + defaults de objetivo/énfasis/free_text.
    supervisor = None
    if payload.supervisor_id:
        supervisor = (
            await session.execute(
                select(Supervisor).where(Supervisor.public_id == payload.supervisor_id)
            )
        ).scalar_one_or_none()
        if supervisor is None:
            raise HTTPException(status_code=404, detail="Supervisor not found")

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
    # Si la auditoría no fijó flujo, hereda el del supervisor.
    if flow_id is None and supervisor is not None and supervisor.flow_id is not None:
        frow = (
            await session.execute(
                select(Flow.id, Flow.name).where(Flow.id == supervisor.flow_id)
            )
        ).one_or_none()
        if frow is not None:
            flow_id, flow_name = frow.id, frow.name

    # Defaults heredados del supervisor cuando el payload no los trae.
    objective = payload.objective or (
        supervisor.default_objective if supervisor else None
    )
    emphasis = payload.emphasis or (
        (supervisor.default_emphasis or []) if supervisor else []
    )
    free_text = payload.free_text or (
        (supervisor.default_free_text or "") if supervisor else ""
    )

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
        supervisor_id=supervisor.id if supervisor else None,
        name=(payload.name or "").strip() or f"Auditoría — {flow_name or 'flujo'}",
        objective=(objective or None),
        provider=(payload.provider or "anthropic"),
        emphasis=emphasis,
        free_text=free_text,
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
                "evaluatedCount": audit.evaluated_count,
                "emphasis": audit.emphasis or [],
                "freeText": audit.free_text or "",
                "createdAt": audit.created_at.isoformat(),
                "status": audit.status,
                "errorMessage": audit.error_message,
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


@router.get("/audits/{audit_public_id}/export.csv")
async def export_audit_csv(
    audit_public_id: str,
    segment: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> StreamingResponse:
    """Exporta las conversaciones de la auditoría como CSV (para campañas).

    Identificadas por conversation_id + external_id. Filtro opcional por segmento
    (cliente_ideal | satisfecho | insatisfecho | potencial_lead | ...)."""
    audit_id = (
        await session.execute(
            select(Audit.id).where(Audit.public_id == audit_public_id)
        )
    ).scalar_one_or_none()
    if audit_id is None:
        raise HTTPException(status_code=404, detail="Audit not found")

    rows = (
        await session.execute(
            select(Conversation, Evaluation)
            .join(
                AuditConversation,
                AuditConversation.conversation_id == Conversation.id,
            )
            .outerjoin(Evaluation, Evaluation.conversation_id == Conversation.id)
            .where(AuditConversation.audit_id == audit_id)
        )
    ).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "conversation_id",
            "external_id",
            "contacto",
            "telefono",
            "segmento",
            "score_final",
            "score",
            "resolucion",
            "resumen",
        ]
    )
    for conv, ev in rows:
        seg = ev.segment if ev is not None else None
        if segment and seg != segment:
            continue
        writer.writerow(
            [
                conv.public_id,
                conv.external_id,
                conv.contact_name or "",
                conv.contact_phone or "",
                seg or "",
                ev.score_final if ev is not None else "",
                ev.score if ev is not None else "",
                ("si" if ev.resolution else "no") if ev is not None else "",
                (ev.summary or "") if ev is not None else "",
            ]
        )
    buf.seek(0)
    filename = f"auditoria_{audit_public_id}{('_' + segment) if segment else ''}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
    # Capa de riesgo (separada del score promedio): "¿tengo que intervenir?".
    risk = {
        "withVeto": 0,
        "needsReview": 0,
        "critical": 0,
        "bySeverity": {"critica": 0, "alta": 0, "media": 0, "baja": 0},
    }
    sev_weight = {"critica": 3, "alta": 2, "media": 1, "baja": 0}
    for conv, ev in conv_rows:
        sat_bucket = None
        if ev is not None:
            if ev.score is not None:
                scores.append(ev.score)
            sat_bucket = _SATISFACTION_BUCKETS.get(ev.satisfaction or 0, "insatisfecho")
            buckets[sat_bucket] += 1
        verdicts = me_by_conv.get(conv.id, [])
        for v in verdicts:
            sev = v.get("severity")
            if sev in risk["bySeverity"]:
                risk["bySeverity"][sev] += 1
        has_veto = bool(ev.has_veto) if ev else False
        needs_review = bool(ev.requiere_revision_humana) if ev else False
        problematic = (ev.segment == "problematico") if ev else False
        has_critical_verdict = any(
            v.get("severity") in ("critica", "alta") for v in verdicts
        )
        needs_intervention = (
            has_veto or needs_review or has_critical_verdict or problematic
        )
        if has_veto:
            risk["withVeto"] += 1
        if needs_review:
            risk["needsReview"] += 1
        if needs_intervention:
            risk["critical"] += 1
        # Score de riesgo para ordenar (mayor = más urgente).
        risk_score = 100 if has_veto else 0
        risk_score += sum(sev_weight.get(v.get("severity"), 0) for v in verdicts)
        risk_score += 5 if needs_review else 0
        risk_score += 10 if problematic else 0
        item = {
            "id": conv.public_id,
            "externalId": conv.external_id,
            "contactName": conv.contact_name,
            "preview": conv.preview,
            "messageCount": conv.message_count,
            "score": ev.score if ev else None,
            "satisfaction": sat_bucket,
            "resolved": ev.resolution if ev else None,
            "messageEvaluations": verdicts,
            "needsIntervention": needs_intervention,
            "riskScore": risk_score,
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

    # Las que requieren intervención primero (mayor riesgo arriba).
    conversations.sort(key=lambda c: c["riskScore"], reverse=True)

    return {
        "id": audit.public_id,
        "name": audit.name,
        "objective": audit.objective,
        "flujoId": flow_public_id,
        "flujoName": flow_name,
        "conversationCount": audit.conversation_count,
        "evaluatedCount": audit.evaluated_count,
        "emphasis": audit.emphasis or [],
        "freeText": audit.free_text or "",
        "createdAt": audit.created_at.isoformat(),
        "status": audit.status,
        "errorMessage": audit.error_message,
        "report": {
            "total": len(conversations),
            "satisfaction": buckets,
            "avgScore": round(sum(scores) / len(scores)) if scores else None,
            "risk": risk,
            "failing": failing,
            "conversations": conversations,
            "suggestions": audit.suggestions or [],
        },
    }
