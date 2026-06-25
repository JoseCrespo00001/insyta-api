"""Dashboard aggregates — overview KPIs, per-project summaries, recent activity.

Maps to the frontend dashboard data (DASHBOARD_OVERVIEW, ProjectSummary[],
ActivityItem[]). Everything is RLS-scoped to the caller's org + allowed projects.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db_with_tenant_context
from app.models import (
    Audit,
    Conversation,
    Evaluation,
    Flow,
    Improvement,
    MessageEvaluation,
    Project,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["dashboard"])

_SATISFACTION_BUCKETS = {5: "satisfecho", 4: "satisfecho", 3: "neutral"}


@router.get("/dashboard", response_model=dict)
async def get_dashboard(
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> dict:
    # --- Overview KPIs (org-wide) ---
    conv_evaluated = (
        await session.execute(select(func.count(Evaluation.id)))
    ).scalar_one()
    avg_score = (
        await session.execute(
            select(func.avg(Evaluation.score)).where(Evaluation.score.isnot(None))
        )
    ).scalar_one()
    audits_run = (await session.execute(select(func.count(Audit.id)))).scalar_one()
    improvements_applied = (
        await session.execute(
            select(func.count(Improvement.id)).where(Improvement.status == "applied")
        )
    ).scalar_one()
    suggestions_open = (
        await session.execute(
            select(func.count(Improvement.id)).where(Improvement.status == "pending")
        )
    ).scalar_one()

    # Satisfaction buckets across all evaluations.
    sat_rows = (await session.execute(select(Evaluation.satisfaction))).scalars().all()
    satisfaction = {"satisfecho": 0, "neutral": 0, "insatisfecho": 0}
    for s in sat_rows:
        satisfaction[_SATISFACTION_BUCKETS.get(s or 0, "insatisfecho")] += 1

    overview = {
        "conversationsEvaluated": int(conv_evaluated or 0),
        "avgScore": round(avg_score) if avg_score is not None else None,
        "auditsRun": int(audits_run or 0),
        "improvementsApplied": int(improvements_applied or 0),
        "suggestionsOpen": int(suggestions_open or 0),
        "satisfaction": satisfaction,
    }

    # --- Datos clave de calidad (los mismos que se ven por conversación) ---
    # Veredictos por mensaje agregados a nivel org: alucinaciones, mensajes
    # marcados (warning/error) y desglose por tipo de problema.
    issue_rows = (
        await session.execute(
            select(MessageEvaluation.issue_type, func.count(MessageEvaluation.id))
            .where(MessageEvaluation.issue_type.isnot(None))
            .group_by(MessageEvaluation.issue_type)
        )
    ).all()
    issues_by_type = {str(t): int(c) for t, c in issue_rows if t}
    hallucinations = issues_by_type.get("alucinacion", 0)
    flagged_messages = (
        await session.execute(
            select(func.count(MessageEvaluation.id)).where(
                MessageEvaluation.label.in_(["warning", "error"])
            )
        )
    ).scalar_one()
    conversations_with_issues = (
        await session.execute(
            select(func.count(func.distinct(MessageEvaluation.conversation_id))).where(
                MessageEvaluation.label.in_(["warning", "error"])
            )
        )
    ).scalar_one()

    # Potencial de cliente / no resueltas (nivel conversación).
    leads = (
        await session.execute(
            select(func.count(Evaluation.id)).where(
                Evaluation.resolution.is_(True), Evaluation.satisfaction >= 4
            )
        )
    ).scalar_one()
    unresolved = (
        await session.execute(
            select(func.count(Evaluation.id)).where(Evaluation.resolution.is_(False))
        )
    ).scalar_one()

    # Temas más frecuentes (de las evaluaciones a nivel conversación).
    topic_rows = (
        await session.execute(
            select(Evaluation.topic, func.count(Evaluation.id))
            .where(Evaluation.topic.isnot(None))
            .group_by(Evaluation.topic)
            .order_by(func.count(Evaluation.id).desc())
            .limit(6)
        )
    ).all()
    topics = [{"topic": str(t), "count": int(c)} for t, c in topic_rows if t]

    quality = {
        "hallucinations": int(hallucinations or 0),
        "flaggedMessages": int(flagged_messages or 0),
        "conversationsWithIssues": int(conversations_with_issues or 0),
        "leads": int(leads or 0),
        "unresolved": int(unresolved or 0),
        "issuesByType": issues_by_type,
        "topics": topics,
    }

    # --- Per-project summaries ---
    projects = (
        (await session.execute(select(Project).order_by(Project.created_at.desc())))
        .scalars()
        .all()
    )
    project_summaries = []
    for p in projects:
        score = (
            await session.execute(
                select(func.avg(Evaluation.score)).where(
                    Evaluation.project_id == p.id, Evaluation.score.isnot(None)
                )
            )
        ).scalar_one()
        conv_count = (
            await session.execute(
                select(func.count(Conversation.id)).where(
                    Conversation.project_id == p.id
                )
            )
        ).scalar_one()
        flow_count = (
            await session.execute(
                select(func.count(Flow.id)).where(Flow.project_id == p.id)
            )
        ).scalar_one()
        open_sugg = (
            await session.execute(
                select(func.count(Improvement.id)).where(
                    Improvement.project_id == p.id, Improvement.status == "pending"
                )
            )
        ).scalar_one()
        running = (
            await session.execute(
                select(func.count(Audit.id)).where(
                    Audit.project_id == p.id, Audit.status == "running"
                )
            )
        ).scalar_one()
        last_audit = (
            await session.execute(
                select(func.max(Audit.created_at)).where(Audit.project_id == p.id)
            )
        ).scalar_one()
        project_summaries.append(
            {
                "publicId": p.public_id,
                "name": p.name,
                "score": round(score) if score is not None else None,
                "conversations": int(conv_count or 0),
                "flujos": int(flow_count or 0),
                "suggestionsOpen": int(open_sugg or 0),
                "auditStatus": "running" if running else "idle",
                "lastAuditAt": last_audit.isoformat() if last_audit else None,
            }
        )

    # --- Recent activity (audits + improvements) ---
    recent = []
    recent_audits = (
        await session.execute(
            select(Audit.public_id, Audit.name, Audit.created_at, Audit.status)
            .order_by(Audit.created_at.desc())
            .limit(8)
        )
    ).all()
    for a in recent_audits:
        recent.append(
            {
                "id": a.public_id,
                "kind": "audit",
                "text": f"Auditoría '{a.name}' ({a.status})",
                "at": a.created_at.isoformat(),
            }
        )
    recent.sort(key=lambda x: x["at"], reverse=True)

    return {
        "overview": overview,
        "quality": quality,
        "projectSummaries": project_summaries,
        "recentActivity": recent[:10],
    }
