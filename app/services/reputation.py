"""Actualización de reputación acumulada (rúbrica §6, AUD-4.2).

Al terminar una auditoría se actualiza la reputación del agente (media móvil del
score, conteo de VETO) y de cada usuario (sentimiento histórico, señales de lead
y fraude). Upsert idempotente por agente / (proyecto, user_key).
"""

from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentReputation, Evaluation, UserReputation
from app.services.spc import spc_summary

_USER_SALT = "insyta-userrep-v1"


def hash_user_key(external_id: str) -> str:
    """Hash estable del external_id (no guardamos el número/teléfono crudo)."""
    return hashlib.sha256(f"{_USER_SALT}:{external_id}".encode()).hexdigest()[:64]


def client_pseudonym(external_id: str) -> str:
    """Identificador pseudónimo estable para mostrar/exportar sin PII (B5):
    `cliente #<6 chars del hash>`. No es reversible a teléfono."""
    return f"cliente #{hash_user_key(external_id)[:6]}"


def merge_avg(old_avg: Decimal | float | None, old_count: int, value: float) -> float:
    """Media móvil incremental."""
    if not old_count or old_avg is None:
        return float(value)
    return (float(old_avg) * old_count + value) / (old_count + 1)


def risk_label(fraud_attempts: int, avg_sentiment: float | None, is_lead: bool) -> str:
    """Etiqueta de usuario a partir de las señales acumuladas."""
    if fraud_attempts >= 2:
        return "problematico"
    if is_lead:
        return "lead_calificado"
    if avg_sentiment is not None and avg_sentiment >= 4:
        return "recurrente"
    return "neutral"


async def update_agent_spc(session: AsyncSession, *, agent_id: uuid.UUID) -> None:
    """Recalcula baseline SPC + tendencia del agente sobre su historial de scores
    y lo persiste en agent_reputation (deriva → alerta en el reporte)."""
    scores = (
        (
            await session.execute(
                select(Evaluation.score)
                .where(Evaluation.agent_id == agent_id, Evaluation.score.is_not(None))
                .order_by(Evaluation.evaluated_at.asc().nulls_last())
            )
        )
        .scalars()
        .all()
    )
    summary = spc_summary([float(s) for s in scores if s is not None])
    if summary is None:
        return
    rep = (
        await session.execute(select(AgentReputation).where(AgentReputation.agent_id == agent_id))
    ).scalar_one_or_none()
    if rep is None:
        return
    b = summary.baseline
    rep.baseline_mean = Decimal(str(round(b.mean, 2)))
    rep.baseline_std = Decimal(str(round(b.std, 3)))
    rep.ucl = Decimal(str(round(b.ucl, 2)))
    rep.lcl = Decimal(str(round(b.lcl, 2)))
    rep.trend = summary.trend


def user_note(usuario_riesgoso: bool, fraud_attempts: int) -> str | None:
    """Nota de contexto para el judge según el historial del usuario (o None)."""
    if usuario_riesgoso or fraud_attempts > 0:
        return (
            "HISTORIAL DEL USUARIO: marcado como riesgoso "
            f"({fraud_attempts} intento(s) de fraude previos). Leé sus mensajes con "
            "más suspicacia (comprobantes, CBU, presión por descuentos)."
        )
    return None


async def get_user_note(
    session: AsyncSession, *, project_id: uuid.UUID, external_id: str
) -> str | None:
    """Busca la reputación del usuario y devuelve una nota para el judge (o None)."""
    rep = (
        await session.execute(
            select(UserReputation.usuario_riesgoso, UserReputation.fraud_attempts).where(
                UserReputation.project_id == project_id,
                UserReputation.user_key == hash_user_key(external_id),
            )
        )
    ).one_or_none()
    if rep is None:
        return None
    return user_note(rep.usuario_riesgoso, rep.fraud_attempts)


async def update_agent_reputation(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    score: float | None,
    has_veto: bool,
) -> None:
    rep = (
        await session.execute(select(AgentReputation).where(AgentReputation.agent_id == agent_id))
    ).scalar_one_or_none()
    if rep is None:
        rep = AgentReputation(
            id=uuid.uuid4(),
            public_id=f"rep_agt_{uuid.uuid4().hex[:20]}",
            org_id=org_id,
            project_id=project_id,
            agent_id=agent_id,
            # Inicializar en 0: el default de SQLAlchemy recién aplica en el flush,
            # y acá incrementamos antes de flushear (None += 1 explotaría).
            score_count=0,
            veto_count=0,
        )
        session.add(rep)
    if score is not None:
        rep.avg_score = Decimal(str(round(merge_avg(rep.avg_score, rep.score_count, score), 2)))
        rep.score_count += 1
    if has_veto:
        rep.veto_count += 1


async def update_user_reputation(
    session: AsyncSession,
    *,
    external_id: str,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    sentiment: float | None,
    is_lead: bool,
    is_fraud: bool,
) -> None:
    user_key = hash_user_key(external_id)
    rep = (
        await session.execute(
            select(UserReputation).where(
                UserReputation.project_id == project_id,
                UserReputation.user_key == user_key,
            )
        )
    ).scalar_one_or_none()
    if rep is None:
        rep = UserReputation(
            id=uuid.uuid4(),
            public_id=f"rep_usr_{uuid.uuid4().hex[:20]}",
            org_id=org_id,
            project_id=project_id,
            user_key=user_key,
            # Inicializar en 0 (ver nota en update_agent_reputation).
            sentiment_count=0,
            fraud_attempts=0,
        )
        session.add(rep)
    if sentiment is not None:
        rep.avg_sentiment = Decimal(
            str(round(merge_avg(rep.avg_sentiment, rep.sentiment_count, sentiment), 2))
        )
        rep.sentiment_count += 1
    if is_fraud:
        rep.fraud_attempts += 1
    rep.usuario_riesgoso = rep.fraud_attempts >= 2
    rep.etiqueta = risk_label(
        rep.fraud_attempts,
        float(rep.avg_sentiment) if rep.avg_sentiment is not None else None,
        is_lead,
    )


__all__ = [
    "get_user_note",
    "hash_user_key",
    "merge_avg",
    "risk_label",
    "update_agent_reputation",
    "update_agent_spc",
    "update_user_reputation",
    "user_note",
]
