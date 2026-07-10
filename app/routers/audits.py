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
    Message,
    MessageEvaluation,
    Project,
    Supervisor,
)
from app.services.celery_app import celery_app
from app.services.report_format import eval_to_camel
from app.services.report_metrics import (
    content_signature,
    is_phone_like,
    needs_intervention,
    satisfaction_bucket,
    visible_score,
)
from app.services.reputation import client_pseudonym

# Motivo en lenguaje humano de por qué una conversación requiere intervención (B3).
_VETO_LABELS = {
    "A1_alucinacion": "Alucinación factual",
    "A2_riesgo_legal": "Riesgo legal",
    "A3_pii": "Fuga de datos (PII)",
    "A4_scope": "Fuera de alcance",
    "A5_cbu_invalido": "CBU inválido",
}
_ISSUE_LABELS = {
    "alucinacion": "Alucinación",
    "alcance": "Fuera de alcance",
    "contradiccion": "Contradicción",
    "error_politica": "Error de política",
}


def _human_reason(ev: Evaluation | None, verdicts: list[dict]) -> str | None:
    """Por qué hay que intervenir, en lenguaje humano (calculado en el back — el
    front lo consume, no lo recalcula)."""
    if ev is not None and ev.has_veto and ev.veto_flags:
        firm = (ev.rubric or {}).get("veto_firm", True)
        tag = "" if firm else " (a confirmar)"
        parts = [
            _VETO_LABELS.get(str(f), str(f).replace("_", " ")) for f in ev.veto_flags
        ]
        return " · ".join(parts) + tag
    sev = [v for v in verdicts if v.get("severity") in ("critica", "alta")]
    if sev:
        it = str(sev[0].get("issueType") or "")
        label = _ISSUE_LABELS.get(it) or it or "problema"
        return f"{len(sev)} {label} ({sev[0].get('severity')})"
    if ev is not None and ev.requiere_revision_humana:
        return "Requiere revisión humana"
    if ev is not None and ev.segment == "problematico":
        return "Conversación problemática"
    return None


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["audits"])


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
                    # Data usable para el card de la lista (ya persistida en el
                    # report_summary): score visible promedio, ataques repelidos/
                    # cedidos y resolución. Ausentes en auditorías failed/running.
                    "avgScore": summary.get("avgScore"),
                    "adversarial": summary.get("adversarial"),
                    "resolution": summary.get("resolution"),
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
            "cliente",  # B5: pseudónimo, nunca el teléfono crudo
            "contacto",
            "segmento",
            "score",  # visible (topeado por VETO firme, B1)
            "score_bruto",
            "resolucion",
            "resumen",
        ]
    )
    for conv, ev in rows:
        seg = ev.segment if ev is not None else None
        if segment and seg != segment:
            continue
        name = conv.contact_name if not is_phone_like(conv.contact_name) else ""
        writer.writerow(
            [
                conv.public_id,
                client_pseudonym(conv.external_id),
                name or "",
                seg or "",
                visible_score(ev.score, ev.score_final) if ev is not None else "",
                ev.score_bruto if ev is not None else "",
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

    # B4: contenido por conversación para deduplicar los duplicados de determinismo
    # en los agregados (Pareto/riesgo). Se cargan una vez, ordenados por seq.
    conv_ids = [conv.id for conv, _ in conv_rows]
    content_by_conv: dict[uuid.UUID, list[str]] = {}
    if conv_ids:
        crows = await session.execute(
            select(Message.conversation_id, Message.content)
            .where(Message.conversation_id.in_(conv_ids))
            .order_by(Message.conversation_id, Message.seq)
        )
        for cr in crows:
            content_by_conv.setdefault(cr.conversation_id, []).append(cr.content or "")

    conversations = []
    failing = []
    buckets = {"satisfecho": 0, "neutral": 0, "insatisfecho": 0}  # fallback B2
    scores = []  # fallback avgScore
    # Capa de riesgo (separada del score promedio): "¿tengo que intervenir?".
    by_severity: dict[str, int] = {"critica": 0, "alta": 0, "media": 0, "baja": 0}
    with_veto = 0
    needs_review_count = 0
    critical_count = 0
    sev_weight = {"critica": 3, "alta": 2, "media": 1, "baja": 0}
    seen_sigs: set[str] = set()  # B4
    # Eje adversarial (Prompt 3/4): contador live de ataques (repelidos vs cedidos),
    # fallback si el summary persistido no está. Separado del score de calidad.
    adv_live: dict = {"total": 0, "repelled": 0, "ceded": 0, "byType": {}}
    for conv, ev in conv_rows:
        verdicts = me_by_conv.get(conv.id, [])
        # Reporte POR AUDITORÍA: solo las conversaciones que ESTA auditoría evaluó
        # (verdicts con su audit_id). Evita que dos auditorías sobre las mismas
        # conversaciones muestren el mismo reporte (Evaluation es única por conv).
        if not verdicts:
            continue
        # B4: duplicados de determinismo → visibles en la lista pero contados una vez.
        sig = content_signature(content_by_conv.get(conv.id, [conv.preview or ""]))
        is_dup = sig in seen_sigs
        seen_sigs.add(sig)

        visible = visible_score(ev.score, ev.score_final) if ev else None  # B1
        is_adv = bool(ev.is_adversarial) if ev else False
        sat_bucket = None
        # A3: los ataques NO entran en el promedio de satisfacción/score; se
        # cuentan aparte como repelidos/cedidos.
        if ev is not None and not is_adv:
            if visible is not None:
                scores.append(visible)
            sat_bucket = satisfaction_bucket(ev.satisfaction)
            buckets[sat_bucket] += 1
        if ev is not None and is_adv and not is_dup:
            adv_live["total"] += 1
            if ev.attack_repelled:
                adv_live["repelled"] += 1
            else:
                adv_live["ceded"] += 1
            at = ev.attack_type or "otro"
            adv_live["byType"][at] = adv_live["byType"].get(at, 0) + 1
        has_veto = bool(ev.has_veto) if ev else False
        needs_review = bool(ev.requiere_revision_humana) if ev else False
        problematic = (ev.segment == "problematico") if ev else False
        has_critical_verdict = any(
            v.get("severity") in ("critica", "alta") for v in verdicts
        )
        intervention = needs_intervention(
            has_veto=has_veto,
            needs_review=needs_review,
            has_critical_verdict=has_critical_verdict,
            problematic=problematic,
        )
        if not is_dup:
            for v in verdicts:
                sev = v.get("severity")
                if sev in by_severity:
                    by_severity[sev] += 1
            if has_veto:
                with_veto += 1
            if needs_review:
                needs_review_count += 1
            if intervention:
                critical_count += 1
        # Score de riesgo para ordenar (mayor = más urgente).
        risk_score = 100 if has_veto else 0
        risk_score += sum(sev_weight.get(v.get("severity"), 0) for v in verdicts)
        risk_score += 5 if needs_review else 0
        risk_score += 10 if problematic else 0
        # B5: identificador pseudónimo — nunca el teléfono crudo.
        pseudonym = client_pseudonym(conv.external_id)
        name = conv.contact_name if not is_phone_like(conv.contact_name) else None
        item = {
            "id": conv.public_id,
            "externalId": pseudonym,
            "contactName": name or pseudonym,
            "preview": conv.preview,
            "messageCount": conv.message_count,
            "score": visible,  # B1: score visible (topeado por VETO firme)
            "satisfaction": sat_bucket,
            "resolved": ev.resolution if ev else None,
            "messageEvaluations": verdicts,
            "needsIntervention": intervention,
            "reason": _human_reason(ev, verdicts),  # B3: motivo en lenguaje humano
            "riskScore": risk_score,
            "isDuplicate": is_dup,  # B4: visible pero no contado en agregados
            "uploadGroupId": str(conv.upload_id) if conv.upload_id else "",
            "userMessages": 0,
            "botMessages": 0,
            "messages": [],
            "selected": False,
            "pinned": False,
            "evaluation": eval_to_camel(ev),
        }
        conversations.append(item)
        # B3: failing = las que requieren intervención (VETO/revisión/crítica), no
        # solo "no resueltas".
        if intervention:
            failing.append(item)

    conversations.sort(key=lambda c: c["riskScore"], reverse=True)
    failing.sort(key=lambda c: c["riskScore"], reverse=True)

    summary = audit.report_summary or {}
    risk = {
        "withVeto": with_veto,
        "needsReview": needs_review_count,
        "critical": critical_count,
        "bySeverity": by_severity,
        # Eje adversarial (Prompt 3/4): ataques repelidos vs cedidos, separado del
        # score de calidad. Preferimos el summary persistido; fallback al live.
        "adversarial": summary.get("adversarial") or adv_live,
    }

    # B2: satisfacción + avgScore desde el resumen persistido (fuente ÚNICA que
    # también consume list_audits → card y chips muestran lo mismo). Fallback live.
    satisfaction = summary.get("satisfaction") or buckets
    avg_score = summary.get("avgScore")
    if avg_score is None:
        avg_score = round(sum(scores) / len(scores)) if scores else None
    # A4/A5: resolución (denominador = conversaciones legítimas) y escaladas
    # correctas vs evitables — server-authoritative desde el summary.
    resolution = summary.get("resolution")
    escalations = summary.get("escalations")

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
            "satisfaction": satisfaction,
            "avgScore": avg_score,
            "resolution": resolution,  # A4: denominador = conversaciones legítimas
            "escalations": escalations,  # A5: correctas vs evitables
            "risk": risk,
            "failing": failing,
            "conversations": conversations,
            "suggestions": audit.suggestions or [],
        },
    }
