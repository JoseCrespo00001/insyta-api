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
import json
import logging
import uuid
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import engine, tenant_txn
from app.llm.audit_judge import judge_messages
from app.llm.flow_audit import (
    generate_flowless_suggestions,
    propose_flow_changes,
    summarize_flow,
)
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
    Supervisor,
)
from app.services.celery_app import celery_app
from app.services.fraud import detect_fraud, fraud_flag_names
from app.services.notifications import create_notification
from app.services.report_metrics import (
    content_signature,
    satisfaction_distribution,
    visible_score,
)
from app.services.reputation import (
    get_user_note,
    update_agent_reputation,
    update_agent_spc,
    update_user_reputation,
)
from app.services.rubric_mapping import map_eval_to_rubric
from app.services.rubric_scoring import compute_scores
from app.services.segmentation import derive_segment
from app.services.validators.cbu import find_cbus, validate_cbu
from app.services.validators.prices import check_prices

logger = logging.getLogger(__name__)

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
        select(AuditConversation.conversation_id).where(AuditConversation.audit_id == audit_id)
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


# Prompt 2/4: cuántos ejemplos anonimizados guardamos por patrón para el LLM.
_EXAMPLES_PER_ISSUE = 2


def _build_suggestions(issue_counter: Counter, min_messages: int = 1) -> list[dict]:
    """Sugerencias templadas (baseline / fallback). S2: filtra categorías con menos
    de `min_messages` (mata "Reducir casos de otro, 1 mensaje")."""
    suggestions = []
    for issue_type, count in issue_counter.most_common(3):
        if not issue_type or count < min_messages:
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


def _flowless_stats(issue_counter: Counter, min_messages: int, *, limit: int = 3) -> list[dict]:
    """Patrones accionables para el generador sin-flujo (S1/S2): descarta keys
    vacías y `otro` (catch-all inaccionable), mantiene `count >= min_messages`,
    ordena por count desc y corta a `limit`. Incluye `fraude:*` (el parche
    anti-jailbreak es alto valor). `label` = sin prefijo `fraude:` y `_`→espacio."""
    stats: list[dict] = []
    for issue_type, count in issue_counter.most_common():
        if not issue_type or issue_type == "otro" or count < min_messages:
            continue
        label = issue_type.removeprefix("fraude:").replace("_", " ")
        stats.append({"issue_type": issue_type, "label": label, "count": count})
        if len(stats) >= limit:
            break
    return stats


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


async def _compute_report_summary(session: AsyncSession, conv_ids: list[uuid.UUID]) -> dict:
    """Fuente ÚNICA del resumen (B2): satisfacción + avgScore. avgScore usa el
    score VISIBLE (topeado por VETO, B1), no el crudo."""
    if not conv_ids:
        return {"total": 0, "satisfaction": {}, "avgScore": None}
    result = await session.execute(
        select(Evaluation.score, Evaluation.score_final, Evaluation.satisfaction).where(
            Evaluation.conversation_id.in_(conv_ids)
        )
    )
    rows = result.all()
    scores = [s for r in rows if (s := visible_score(r.score, r.score_final)) is not None]
    buckets = satisfaction_distribution([r.satisfaction for r in rows])
    avg = round(sum(scores) / len(scores)) if scores else None
    return {"total": len(conv_ids), "satisfaction": buckets, "avgScore": avg}


def _det_veto(msgs: list[dict], precios: object) -> list[str]:
    """Flags VETO deterministas desde los mensajes del BOT: precio inventado y
    CBU inválido dado por el agente."""
    veto: list[str] = []
    bot_text = " ".join(
        m["content"] for m in msgs if m.get("role") == "assistant" and m.get("content")
    )
    if precios and not check_prices(bot_text, precios).ok:
        veto.append("A1_alucinacion")
    for m in msgs:
        if m.get("role") == "assistant":
            for cbu in find_cbus(m.get("content") or ""):
                if not validate_cbu(cbu):
                    veto.append("A5_cbu_invalido")
    return sorted(set(veto))


async def _persist_rubric_columns(
    session: AsyncSession, conv_id: uuid.UUID, rubric, score, segment: str
) -> None:
    await session.execute(
        update(Evaluation)
        .where(Evaluation.conversation_id == conv_id)
        .values(
            # veto_firm va DENTRO del JSON de rúbrica (sin migración, B7); el front
            # lo lee para distinguir VETO firme vs tentativo.
            rubric={**rubric.model_dump(), "veto_firm": score.veto_firm},
            score_bruto=score.score_bruto,
            score_final=score.score_final,
            confidence=score.confidence,
            has_veto=score.has_veto,
            veto_flags=score.veto_flags,
            segment=segment,
            sentiment_trajectory=rubric.sentimiento_trayectoria,
            requiere_revision_humana=rubric.requiere_revision_humana,
        )
    )


def _format_source_of_truth(attached_data: dict | None) -> str | None:
    """Serializa la data adjunta del supervisor (precios.json/info.json/…) como
    un bloque de FUENTE DE VERDAD para el judge. El judge NO debe marcar como
    alucinación lo que coincide con estos datos."""
    if not attached_data:
        return None
    parts: list[str] = []
    for key in sorted(attached_data.keys()):
        value = attached_data[key]
        try:
            rendered = json.dumps(value, ensure_ascii=False, indent=2)[:4000]
        except (TypeError, ValueError):
            rendered = str(value)[:4000]
        parts.append(f"[{key}]\n{rendered}")
    if not parts:
        return None
    return (
        "FUENTE DE VERDAD (datos autoritativos del negocio — verificá precios, "
        "stock, plazos y datos contra esto; NO marques como alucinación lo que "
        "coincide con estos datos; solo marcá alucinación si el bot afirma algo "
        "que CONTRADICE o que NO está respaldado por esta fuente ni por el flujo):\n"
        + "\n\n".join(parts)
    )


async def _run(audit_id: uuid.UUID, org_id: uuid.UUID) -> dict:
    async with tenant_txn(org_id) as session:
        audit, conv_ids = await _load_audit_conversations(session, audit_id)
        emphasis = audit.emphasis or []
        free_text = audit.free_text
        flow_id = audit.flow_id
        project_id = audit.project_id
        audit_name = audit.name
        objective = audit.objective
        provider = audit.provider or "anthropic"
        logger.info(
            "[AUDIT] start audit=%s org=%s provider=%s convs=%d",
            audit_id,
            org_id,
            provider,
            len(conv_ids),
        )
        # API keys del tenant (cifradas) según el motor elegido.
        org = await session.get(Organization, org_id)
        org_key_enc = org.anthropic_api_key_encrypted if org else None
        org_ds_enc = org.deepseek_api_key_encrypted if org else None
        # Contexto para el judge: objetivo + knowledge/fuente de verdad + flujo.
        # El Supervisor (si la auditoría lo eligió) es el cerebro: aporta
        # knowledge_base + attached_data (fuente de verdad) y puede aportar el flow.
        supervisor = (
            await session.get(Supervisor, audit.supervisor_id) if audit.supervisor_id else None
        )
        project = await session.get(Project, project_id)
        project_public_id = project.public_id if project else None
        # knowledge del supervisor pisa el company_context legacy del proyecto.
        company_context = (
            supervisor.knowledge_base if supervisor and supervisor.knowledge_base else None
        ) or (project.company_context if project else None)
        attached_data = supervisor.attached_data if supervisor else None
        source_of_truth = _format_source_of_truth(attached_data)
        attached_precios = (attached_data or {}).get("precios")
        # El flow puede venir del supervisor si la auditoría no fijó uno propio.
        effective_flow_id = flow_id or (supervisor.flow_id if supervisor else None)
        flow_summary = None
        if effective_flow_id is not None:
            flow = await session.get(Flow, effective_flow_id)
            if flow is not None and flow.flow_json:
                flow_summary = summarize_flow(flow.flow_json)[:2500]

    objective_label = OBJECTIVE_LABELS.get(objective or "", objective)
    ctx_parts: list[str] = []
    if objective_label:
        ctx_parts.append(f"OBJETIVO DE LA CAMPAÑA: {objective_label}")
    if company_context:
        ctx_parts.append(f"DATOS DE LA EMPRESA:\n{company_context}")
    if source_of_truth:
        ctx_parts.append(source_of_truth)
    if flow_summary:
        ctx_parts.append(f"FLUJO ESPERADO (lo que el agente debería hacer):\n{flow_summary}")
    eval_context = "\n\n".join(ctx_parts) or None
    logger.info(
        "[AUDIT] contexto audit=%s supervisor=%s fuente_verdad=%s flow=%s objetivo=%s ctx_chars=%d",
        audit_id,
        supervisor is not None,
        source_of_truth is not None,
        flow_summary is not None,
        objective_label or "-",
        len(eval_context or ""),
    )

    if org_key_enc or org_ds_enc:
        from app.llm.credentials import set_llm_keys
        from app.services.secret_crypto import decrypt_secret

        set_llm_keys(
            anthropic=decrypt_secret(org_key_enc) if org_key_enc else None,
            deepseek=decrypt_secret(org_ds_enc) if org_ds_enc else None,
        )

    logger.info(
        "[AUDIT] llm_keys audit=%s anthropic=%s deepseek=%s",
        audit_id,
        bool(org_key_enc),
        bool(org_ds_enc),
    )

    router = build_router(provider)
    issue_counter: Counter = Counter()
    critical_count = 0  # convs con VETO / fraude / segmento problemático
    evaluated = 0
    msg_evals = 0
    # Punto #6: conversaciones que pidieron un camino no cubierto por el flujo.
    unhandled: list[dict] = []
    unhandled_seen: set = set()
    # Prompt 2/4: ejemplos ANONIMIZADOS por issue_type, para la evidencia del LLM.
    examples_by_issue: dict[str, list[dict]] = {}
    agent_ids: set[uuid.UUID] = set()
    # Acumuladores para la línea de métricas GQM al cierre (costo/tokens/cache).
    cost_total = 0.0
    tokens_total = 0
    latency_total = 0
    cache_read_total = 0
    input_total = 0
    processed = 0  # progreso: conversaciones procesadas (para la barra del front)
    seen_sigs: set[str] = set()  # B4: firmas de contenido ya contadas (dedup)

    logger.info("[AUDIT] loop audit=%s convs=%d", audit_id, len(conv_ids))
    for conv_id in conv_ids:
        async with tenant_txn(org_id) as session:
            conv = await session.get(Conversation, conv_id)
            if conv is None:
                continue
            agent_ids.add(conv.agent_id)
            msgs = await _load_messages(session, conv_id)
            seq_to_msg_id = {m["seq"]: m["id"] for m in msgs}
            seq_to_anon = {m["seq"]: m["content_anonymized"] for m in msgs}
            anon_count = sum(1 for m in msgs if m["content_anonymized"])
            # B4: los duplicados de determinismo se evalúan y quedan visibles, pero
            # cuentan UNA vez en los agregados (issue_counter → Pareto/sugerencias).
            conv_sig = content_signature([m["content"] or "" for m in msgs])
            is_dup = conv_sig in seen_sigs
            seen_sigs.add(conv_sig)

            # Historial del usuario (reputación acumulada de auditorías previas):
            # si es riesgoso, el judge lee con más suspicacia.
            user_hist = await get_user_note(
                session, project_id=conv.project_id, external_id=conv.external_id
            )
            conv_context = "\n\n".join(p for p in (eval_context, user_hist) if p) or None
            logger.info(
                "[AUDIT] conv=%s msgs=%d anon=%d/%d user_hist=%s",
                conv.public_id,
                len(msgs),
                anon_count,
                len(msgs),
                user_hist is not None,
            )

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
                    context=conv_context,
                )
                await _persist_conversation_eval(session, conv, parsed, usage)
                evaluated += 1
                new_parsed = parsed  # rúbrica + reputación se computan post-verdicts
                cost_total += float(usage.cost_usd)
                tokens_total += usage.input_tokens + usage.output_tokens
                latency_total += usage.latency_ms
                cache_read_total += usage.cache_read_input_tokens
                input_total += usage.input_tokens
                logger.info(
                    "[AUDIT] eval conv=%s model=%s tokens=%d/%d cost=%s "
                    "latency_ms=%d cache_read=%d score=%d resolution=%s sat=%d tone=%s",
                    conv.public_id,
                    usage.model,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cost_usd,
                    usage.latency_ms,
                    usage.cache_read_input_tokens,
                    parsed.score,
                    parsed.resolution,
                    parsed.satisfaction,
                    parsed.tone,
                )
            else:
                new_parsed = None
                logger.info("[AUDIT] eval conv=%s skip (idempotente)", conv.public_id)

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
                        source_of_truth or "",
                        user_hist or "",
                        f"Flujo esperado:\n{flow_summary}" if flow_summary else "",
                    )
                    if p
                ),
            )
            for v in verdicts:
                if v.issue_type and not is_dup:  # B4: duplicados cuentan una vez
                    issue_counter[v.issue_type] += 1
                    # Prompt 2/4: guardá hasta N ejemplos ANONIMIZADOS por patrón
                    # (solo content_anonymized; si está vacío, se saltea — nunca crudo).
                    snippet = seq_to_anon.get(v.seq)
                    bucket = examples_by_issue.setdefault(v.issue_type, [])
                    if snippet and len(bucket) < _EXAMPLES_PER_ISSUE:
                        bucket.append({"note": v.note or "", "snippet": snippet[:240]})
                # Conversación que pidió algo fuera del flujo / no resuelto.
                # Solo valores del enum real del judge (audit_judge.ISSUE_TYPES);
                # "no_resuelve" no existía en el enum → nunca matcheaba.
                if (
                    v.issue_type in ("alcance", "alucinacion", "contradiccion")
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
            _persisted = await _persist_message_evals(
                session,
                conv=conv,
                audit_id=audit_id,
                verdicts=verdicts,
                seq_to_msg_id=seq_to_msg_id,
            )
            msg_evals += _persisted
            _vissues = Counter(v.issue_type for v in verdicts if v.issue_type)
            logger.info(
                "[AUDIT] verdicts conv=%s n=%d issues=%s persisted=%d",
                conv.public_id,
                len(verdicts),
                dict(_vissues),
                _persisted,
            )
            for v in verdicts:
                if v.label and v.label != "ok":
                    logger.info(
                        "[AUDIT] verdict conv=%s seq=%s label=%s issue=%s sev=%s",
                        conv.public_id,
                        v.seq,
                        v.label,
                        v.issue_type,
                        v.severity,
                    )

            # 3. Rúbrica + reputación (solo en evals nuevos, para no doble-contar).
            if new_parsed is not None:
                fraud_flags = detect_fraud(msgs, precios=attached_precios)
                fraude_names = fraud_flag_names(fraud_flags)
                if not is_dup:  # B4: duplicados cuentan una vez
                    for name in fraude_names:
                        issue_counter[f"fraude:{name}"] += 1
                det_veto = _det_veto(msgs, attached_precios)
                logger.info(
                    "[AUDIT] det conv=%s fraude=%s veto_det=%s",
                    conv.public_id,
                    fraude_names or "-",
                    det_veto or "-",
                )
                last_turn = msgs[-1]["seq"] if msgs else 0
                rubric = map_eval_to_rubric(
                    new_parsed,
                    verdicts,
                    last_turn=last_turn,
                    det_veto=det_veto,
                    fraude_flags=fraude_names,
                )
                score = compute_scores(
                    rubric,
                    det_veto=det_veto,
                    confidence_threshold=get_settings().veto_confidence_threshold,
                )
                segment = derive_segment(rubric, score)
                logger.info(
                    "[AUDIT] rubrica conv=%s score_bruto=%s score_final=%s "
                    "has_veto=%s veto=%s confidence=%s segment=%s",
                    conv.public_id,
                    score.score_bruto,
                    score.score_final,
                    score.has_veto,
                    score.veto_flags or "-",
                    score.confidence,
                    segment,
                )
                if score.has_veto or bool(fraud_flags) or segment == "problematico":
                    critical_count += 1
                await _persist_rubric_columns(session, conv_id, rubric, score, segment)
                is_lead = bool(new_parsed.resolution) and (new_parsed.satisfaction or 0) >= 4
                await update_agent_reputation(
                    session,
                    agent_id=conv.agent_id,
                    org_id=conv.org_id,
                    project_id=conv.project_id,
                    score=new_parsed.score,
                    has_veto=score.has_veto,
                )
                await update_user_reputation(
                    session,
                    external_id=conv.external_id,
                    org_id=conv.org_id,
                    project_id=conv.project_id,
                    sentiment=new_parsed.satisfaction,
                    is_lead=is_lead,
                    is_fraud=bool(fraud_flags),
                )
                logger.info(
                    "[AUDIT] reputacion conv=%s agent=%s is_lead=%s is_fraud=%s",
                    conv.public_id,
                    conv.agent_id,
                    is_lead,
                    bool(fraud_flags),
                )

            # Progreso incremental: la barra del front (polling cada 2500ms) sube
            # conversación a conversación en vez de saltar de 0 a 100.
            processed += 1
            await session.execute(
                update(Audit).where(Audit.id == audit_id).values(evaluated_count=processed)
            )

    # SPC: recalcular baseline + tendencia (deriva) de cada agente auditado.
    if agent_ids:
        async with tenant_txn(org_id) as session:
            for aid in agent_ids:
                await update_agent_spc(session, agent_id=aid)
        logger.info("[AUDIT] spc audit=%s agents=%d", audit_id, len(agent_ids))

    threshold = get_settings().suggestion_min_messages
    # S2: baseline templada, filtrada por umbral (mata "otro, 1 mensaje"). También
    # es el fallback si el generador enriquecido falla.
    suggestions = _build_suggestions(issue_counter, threshold)
    if effective_flow_id is None:
        # Prompt 2/4: SIN flujo cargado → sugerencias accionables de 4 campos
        # (evidencia, causa_probable, parche_prompt pegable, como_verificar)
        # generadas por el LLM a partir de los patrones reales de la corrida.
        stats = _flowless_stats(issue_counter, threshold)
        try:
            rich = await generate_flowless_suggestions(
                stats,
                examples_by_issue,
                source_of_truth=source_of_truth,
                objective=objective_label,
                company_context=company_context,
                provider=provider,
            )
            if rich:
                suggestions = rich
        except Exception as exc:  # no romper la auditoría por las sugerencias
            logger.warning("[AUDIT] generate_flowless_suggestions falló: %s", exc)
    elif flow_id is not None and flow_summary and unhandled:
        # Punto #6: con flujo, el experto en Langflow propone nodos/condiciones/
        # agentes concretos para sumar al flujo (sugerencias estructurales).
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

    logger.info("[AUDIT] sugerencias audit=%s n=%d", audit_id, len(suggestions))

    async with tenant_txn(org_id) as session:
        summary = await _compute_report_summary(session, conv_ids)
        await session.execute(
            update(Audit)
            .where(Audit.id == audit_id)
            .values(
                status="active",
                conversation_count=len(conv_ids),
                evaluated_count=len(conv_ids),
                suggestions=suggestions,
                report_summary=summary,
                finished_at=datetime.now(UTC),
            )
        )
        logger.info(
            "[AUDIT] done audit=%s convs=%d evaluated=%d msg_evals=%d criticas=%d status=active",
            audit_id,
            len(conv_ids),
            evaluated,
            msg_evals,
            critical_count,
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

        # Notificaciones reales para la campana (idempotentes por event_key).
        link = f"/projects/{project_public_id}" if project_public_id else None
        await create_notification(
            session,
            org_id=org_id,
            project_id=project_id,
            kind="audit",
            title="Auditoría completada",
            detail=f"{audit_name} · {len(conv_ids)} conversaciones analizadas.",
            link=link,
            event_key=f"audit_completed:{audit_id}",
        )
        if critical_count > 0:
            plural = "es" if critical_count != 1 else ""
            await create_notification(
                session,
                org_id=org_id,
                project_id=project_id,
                kind="suspicious",
                title=f"{critical_count} conversaci{'ones' if critical_count != 1 else 'ón'} crítica{plural}",
                detail="VETO, fraude o experiencia problemática detectados.",
                link=link,
                event_key=f"audit_critical:{audit_id}",
            )
        if suggestions:
            n_sug = len(suggestions)
            plural = "s" if n_sug != 1 else ""
            await create_notification(
                session,
                org_id=org_id,
                project_id=project_id,
                kind="improvement",
                title=f"{n_sug} sugerencia{plural} nueva{plural}",
                detail=f"{audit_name} · mejoras propuestas.",
                link=link,
                event_key=f"audit_suggestions:{audit_id}",
            )

    latency_avg = latency_total // evaluated if evaluated else 0
    cache_hit = (cache_read_total / input_total) if input_total else 0.0
    logger.info(
        "[AUDIT] metrics audit=%s convs=%d evaluated=%d cost_total=%.6f "
        "tokens_total=%d latency_avg_ms=%d cache_hit=%.2f",
        audit_id,
        len(conv_ids),
        evaluated,
        cost_total,
        tokens_total,
        latency_avg,
        cache_hit,
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
    except Exception as exc:
        # CUALQUIER fallo marca la auditoría "failed" en vez de dejarla clavada en
        # "running" (antes solo se capturaba FatalLLMError, y un bug la colgaba).
        if isinstance(exc, FatalLLMError):
            msg = f"Falta la API key del motor: {exc}"
            logger.warning("[AUDIT] FAILED audit=%s (no LLM key): %s", aid, exc)
        else:
            msg = f"{type(exc).__name__}: {exc}"
            logger.exception("[AUDIT] FAILED audit=%s: %s", aid, exc)

        async def _fail() -> None:
            try:
                async with tenant_txn(oid) as session:
                    await session.execute(
                        update(Audit)
                        .where(Audit.id == aid)
                        .values(status="failed", error_message=msg[:1000])
                    )
            finally:
                await engine.dispose()

        asyncio.run(_fail())
        return {"audit_id": audit_id, "status": "failed", "error": str(exc)}
