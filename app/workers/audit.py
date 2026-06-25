"""run_audit Celery task — runs the LLM-as-judge over an audit's conversations.

For each selected conversation:
  1. Conversation-level evaluation via `LLMRouter.evaluate` -> `evaluations` row
     (idempotent: skipped if the conversation already has one).
  2. Per-message verdicts via `audit_judge.judge_messages` -> `message_evaluations`
     rows (the 'reporte por mensaje'), scoped to this audit.
Then it synthesises flow-level `suggestions` from the issue frequencies and a
`report_summary`, and flips the audit to `active`.

Without a provider key the judge raises FatalLLMError; the audit is marked
`failed` with an explanatory message (no crash).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import engine, tenant_txn
from app.llm.audit_judge import judge_messages
from app.llm.flow_audit import propose_flow_changes, summarize_flow
from app.llm.router import FatalLLMError, build_router
from app.models import (
    Audit,
    AuditConversation,
    Conversation,
    Evaluation,
    Flow,
    Improvement,
    ImprovementConversation,
    Message,
    MessageEvaluation,
    Organization,
    Project,
)
from app.services.celery_app import celery_app

logger = logging.getLogger(__name__)

_SATISFACTION_BUCKETS = {5: "satisfecho", 4: "satisfecho", 3: "neutral"}

# Objetivos de campaña (estilo Meta) → descripción que entiende el judge.
OBJECTIVE_LABELS = {
    "leads": "Recaudar datos / generar leads (pedir y captar nombre, contacto, email/teléfono)",
    "ventas": "Vender / cerrar la conversión (avanzar la compra)",
    "awareness": "Que conozcan la marca / reconocimiento",
    "soporte": "Resolver soporte / atención al cliente",
    "agendar": "Agendar / reservar (turno, demo, llamada)",
    "todos": (
        "Evaluá la conversación contra TODOS los objetivos posibles "
        "(recaudar datos/leads, vender, reconocimiento de marca, soporte y "
        "agendar). El score debe reflejar el desempeño GENERAL across objetivos; "
        "en el summary mencioná en cuáles cumplió y en cuáles no."
    ),
}


async def _load_audit_conversations(
    session: AsyncSession, audit_id: uuid.UUID
) -> tuple[Audit, list[uuid.UUID]]:
    audit = await session.get(Audit, audit_id)
    if audit is None:
        raise ValueError(f"audit {audit_id} not found")
    rows = await session.execute(
        select(AuditConversation.conversation_id).where(
            AuditConversation.audit_id == audit_id
        )
    )
    return audit, [r[0] for r in rows]


async def _load_messages(session: AsyncSession, conv_id: uuid.UUID) -> list[dict]:
    rows = await session.execute(
        select(
            Message.id,
            Message.seq,
            Message.role,
            Message.content,
            Message.content_anonymized,
        )
        .where(Message.conversation_id == conv_id)
        .order_by(Message.seq.asc())
    )
    return [
        {
            "id": r.id,
            "seq": r.seq,
            "role": r.role,
            "content": r.content,
            "content_anonymized": r.content_anonymized,
        }
        for r in rows
    ]


async def _persist_conversation_eval(
    session: AsyncSession, conv: Conversation, parsed, usage
) -> None:
    stmt = (
        pg_insert(Evaluation)
        .values(
            id=uuid.uuid4(),
            public_id=f"eval_{uuid.uuid4().hex[:16]}",
            conversation_id=conv.id,
            project_id=conv.project_id,
            org_id=conv.org_id,
            agent_id=conv.agent_id,
            score=parsed.score,
            resolution=parsed.resolution,
            satisfaction=parsed.satisfaction,
            tone=parsed.tone,
            frustration=parsed.frustration,
            escalated=parsed.escalated,
            efficiency=parsed.efficiency,
            scope_violation=parsed.scope_violation,
            topic=parsed.topic,
            summary=parsed.summary,
            model_used=usage.model,
            tokens_used=usage.input_tokens + usage.output_tokens,
            tokens_input=usage.input_tokens,
            tokens_output=usage.output_tokens,
            cost_usd=usage.cost_usd,
            latency_ms=usage.latency_ms,
            phoenix_span_id=usage.phoenix_span_id,
            evaluated_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=["conversation_id"])
    )
    await session.execute(stmt)


async def _persist_message_evals(
    session: AsyncSession,
    *,
    conv: Conversation,
    audit_id: uuid.UUID,
    verdicts: list,
    seq_to_msg_id: dict[int, uuid.UUID],
) -> int:
    rows = []
    for v in verdicts:
        msg_id = seq_to_msg_id.get(v.seq)
        if msg_id is None:
            continue
        rows.append(
            {
                "id": uuid.uuid4(),
                "public_id": f"meval_{uuid.uuid4().hex[:16]}",
                "message_id": msg_id,
                "conversation_id": conv.id,
                "audit_id": audit_id,
                "project_id": conv.project_id,
                "org_id": conv.org_id,
                "label": v.label,
                "issue_type": v.issue_type,
                "issue_subtype": v.issue_subtype,
                "severity": v.severity,
                "note": v.note,
            }
        )
    if rows:
        await session.execute(
            pg_insert(MessageEvaluation)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["audit_id", "message_id"])
        )
    return len(rows)


def _build_suggestions(issue_counter: Counter) -> list[dict]:
    suggestions = []
    for issue_type, count in issue_counter.most_common(3):
        if not issue_type:
            continue
        suggestions.append(
            {
                "title": f"Reducir casos de {issue_type.replace('_', ' ')}",
                "detail": (
                    f"Se detectaron {count} mensajes con '{issue_type}'. "
                    "Ajustá el flujo/prompt para cubrir estos casos."
                ),
                "impact": f"{count} mensajes afectados",
            }
        )
    return suggestions


async def _persist_improvements(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    org_id: uuid.UUID,
    flow_id: uuid.UUID,
    audit_id: uuid.UUID,
    suggestions: list[dict],
    conv_ids: list[uuid.UUID],
) -> None:
    """Create one pending Improvement per suggestion, linked to the flow + audit,
    with the audited conversations as the affected set."""
    for sug in suggestions:
        imp_id = uuid.uuid4()
        session.add(
            Improvement(
                id=imp_id,
                public_id=f"imp_{imp_id.hex[:24]}",
                project_id=project_id,
                org_id=org_id,
                flow_id=flow_id,
                audit_id=audit_id,
                title=sug["title"],
                detail=sug["detail"],
                impact=sug["impact"],
                why=sug["detail"],
                node_json=sug.get("node_json") or None,
                prompt=sug.get("prompt") or None,
                status="pending",
            )
        )
        session.add_all(
            [
                ImprovementConversation(
                    id=uuid.uuid4(),
                    improvement_id=imp_id,
                    conversation_id=cid,
                    project_id=project_id,
                    org_id=org_id,
                )
                for cid in conv_ids
            ]
        )


async def _compute_report_summary(
    session: AsyncSession, conv_ids: list[uuid.UUID]
) -> dict:
    if not conv_ids:
        return {"total": 0, "satisfaction": {}, "avgScore": None}
    rows = await session.execute(
        select(Evaluation.score, Evaluation.satisfaction).where(
            Evaluation.conversation_id.in_(conv_ids)
        )
    )
    scores = []
    buckets = {"satisfecho": 0, "neutral": 0, "insatisfecho": 0}
    for r in rows:
        if r.score is not None:
            scores.append(r.score)
        bucket = _SATISFACTION_BUCKETS.get(r.satisfaction or 0, "insatisfecho")
        buckets[bucket] += 1
    avg = round(sum(scores) / len(scores)) if scores else None
    return {"total": len(conv_ids), "satisfaction": buckets, "avgScore": avg}


async def _run(audit_id: uuid.UUID, org_id: uuid.UUID) -> dict:
    async with tenant_txn(org_id) as session:
        audit, conv_ids = await _load_audit_conversations(session, audit_id)
        emphasis = audit.emphasis or []
        free_text = audit.free_text
        flow_id = audit.flow_id
        project_id = audit.project_id
        objective = audit.objective
        provider = audit.provider or "anthropic"
        # API keys del tenant (cifradas) según el motor elegido.
        org = await session.get(Organization, org_id)
        org_key_enc = org.anthropic_api_key_encrypted if org else None
        org_ds_enc = org.deepseek_api_key_encrypted if org else None
        # Contexto para el judge: objetivo de campaña + datos de empresa + flujo.
        project = await session.get(Project, project_id)
        company_context = project.company_context if project else None
        flow_summary = None
        if flow_id is not None:
            flow = await session.get(Flow, flow_id)
            if flow is not None and flow.flow_json:
                flow_summary = summarize_flow(flow.flow_json)[:2500]

    objective_label = OBJECTIVE_LABELS.get(objective or "", objective)
    ctx_parts: list[str] = []
    if objective_label:
        ctx_parts.append(f"OBJETIVO DE LA CAMPAÑA: {objective_label}")
    if company_context:
        ctx_parts.append(f"DATOS DE LA EMPRESA:\n{company_context}")
    if flow_summary:
        ctx_parts.append(
            f"FLUJO ESPERADO (lo que el agente debería hacer):\n{flow_summary}"
        )
    eval_context = "\n\n".join(ctx_parts) or None

    if org_key_enc or org_ds_enc:
        from app.llm.credentials import set_llm_keys
        from app.services.secret_crypto import decrypt_secret

        set_llm_keys(
            anthropic=decrypt_secret(org_key_enc) if org_key_enc else None,
            deepseek=decrypt_secret(org_ds_enc) if org_ds_enc else None,
        )

    router = build_router(provider)
    issue_counter: Counter = Counter()
    evaluated = 0
    msg_evals = 0
    # Punto #6: conversaciones que pidieron un camino no cubierto por el flujo.
    unhandled: list[dict] = []
    unhandled_seen: set = set()

    for conv_id in conv_ids:
        async with tenant_txn(org_id) as session:
            conv = await session.get(Conversation, conv_id)
            if conv is None:
                continue
            msgs = await _load_messages(session, conv_id)
            seq_to_msg_id = {m["seq"]: m["id"] for m in msgs}

            # 1. Conversation-level eval (skip if already present).
            existing = await session.execute(
                select(Evaluation.id).where(Evaluation.conversation_id == conv_id)
            )
            if existing.scalar_one_or_none() is None:
                parsed, usage = await router.evaluate(
                    [
                        {
                            "role": m["role"],
                            "content": m["content"],
                            "content_anonymized": m["content_anonymized"],
                        }
                        for m in msgs
                    ],
                    context=eval_context,
                )
                await _persist_conversation_eval(session, conv, parsed, usage)
                evaluated += 1

            # 2. Per-message verdicts (con objetivo + empresa + flujo).
            verdicts = await judge_messages(
                msgs,
                emphasis=emphasis,
                free_text=free_text,
                objective=objective_label,
                provider=provider,
                flow_context="\n\n".join(
                    p
                    for p in (
                        f"Empresa: {company_context}" if company_context else "",
                        f"Flujo esperado:\n{flow_summary}" if flow_summary else "",
                    )
                    if p
                ),
            )
            for v in verdicts:
                if v.issue_type:
                    issue_counter[v.issue_type] += 1
                # Conversación que pidió algo fuera del flujo / no resuelto.
                if (
                    v.issue_type
                    in ("alcance", "no_resuelve", "alucinacion", "contradiccion")
                    and conv_id not in unhandled_seen
                ):
                    unhandled_seen.add(conv_id)
                    unhandled.append(
                        {
                            "contact": conv.contact_name or conv.external_id,
                            "preview": conv.preview or "",
                            "note": v.note or v.issue_type,
                        }
                    )
            msg_evals += await _persist_message_evals(
                session,
                conv=conv,
                audit_id=audit_id,
                verdicts=verdicts,
                seq_to_msg_id=seq_to_msg_id,
            )

    suggestions = _build_suggestions(issue_counter)
    # Punto #6: a partir de las conversaciones no cubiertas, el experto en
    # Langflow propone nodos/condiciones/agentes concretos para sumar al flujo.
    if flow_id is not None and flow_summary and unhandled:
        try:
            structural = await propose_flow_changes(
                flow_summary,
                unhandled,
                objective=objective_label,
                company_context=company_context,
                provider=provider,
            )
            # Estructurales primero (son las más accionables para el flujo).
            suggestions = structural + suggestions
        except Exception as exc:  # no romper la auditoría por las sugerencias
            logger.warning("[AUDIT] propose_flow_changes falló: %s", exc)

    async with tenant_txn(org_id) as session:
        summary = await _compute_report_summary(session, conv_ids)
        await session.execute(
            update(Audit)
            .where(Audit.id == audit_id)
            .values(
                status="active",
                conversation_count=len(conv_ids),
                suggestions=suggestions,
                report_summary=summary,
                finished_at=datetime.now(UTC),
            )
        )
        # Turn each suggestion into a per-flow Improvement (pending) so the
        # dashboard's /improvements view can surface them next to the flow.
        if flow_id is not None and suggestions:
            await _persist_improvements(
                session,
                project_id=project_id,
                org_id=org_id,
                flow_id=flow_id,
                audit_id=audit_id,
                suggestions=suggestions,
                conv_ids=conv_ids,
            )

    return {
        "audit_id": str(audit_id),
        "conversations": len(conv_ids),
        "evaluated": evaluated,
        "message_evaluations": msg_evals,
    }


async def _run_and_dispose(aid: uuid.UUID, oid: uuid.UUID) -> dict:
    try:
        return await _run(aid, oid)
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.audit.run_audit", bind=True)
def run_audit(self, audit_id: str, org_id: str) -> dict:
    aid = uuid.UUID(audit_id)
    oid = uuid.UUID(org_id)
    try:
        return asyncio.run(_run_and_dispose(aid, oid))
    except FatalLLMError as exc:
        logger.warning("[AUDIT] no LLM key, marking audit %s failed: %s", aid, exc)

        async def _fail() -> None:
            try:
                async with tenant_txn(oid) as session:
                    await session.execute(
                        update(Audit)
                        .where(Audit.id == aid)
                        .values(status="failed", error_message=str(exc))
                    )
            finally:
                await engine.dispose()

        asyncio.run(_fail())
        return {"audit_id": audit_id, "status": "failed", "error": str(exc)}
