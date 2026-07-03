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

from app.models import AgentReputation, UserReputation

_USER_SALT = "insyta-userrep-v1"


def hash_user_key(external_id: str) -> str:
    """Hash estable del external_id (no guardamos el número/teléfono crudo)."""
    return hashlib.sha256(f"{_USER_SALT}:{external_id}".encode()).hexdigest()[:64]


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
        await session.execute(
            select(AgentReputation).where(AgentReputation.agent_id == agent_id)
        )
    ).scalar_one_or_none()
    if rep is None:
        rep = AgentReputation(
            id=uuid.uuid4(),
            public_id=f"rep_agt_{uuid.uuid4().hex[:20]}",
            org_id=org_id,
            project_id=project_id,
            agent_id=agent_id,
        )
        session.add(rep)
    if score is not None:
        rep.avg_score = Decimal(
            str(round(merge_avg(rep.avg_score, rep.score_count, score), 2))
        )
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
    "hash_user_key",
    "merge_avg",
    "risk_label",
    "update_agent_reputation",
    "update_user_reputation",
]
