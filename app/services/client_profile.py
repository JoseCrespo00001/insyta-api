"""Perfil por cliente: agrega lo persistido por (project, external_id) para que
el dueño del agente conozca a su cliente y personalice la atención.

Reusa `UserReputation` (respetuoso/etiqueta/sentimiento) + evaluaciones (temas,
score, segmento) + timestamps de mensajes (a qué hora responde). Todas las queries
corren bajo el contexto de tenant (RLS) del caller.

Caveats declarados:
- "Horas de respuesta" salen de Message.timestamp (role='user'), en **UTC** (no la
  hora local del cliente — tz local = futuro). Se filtran timestamps epoch (~1970),
  que son el fallback de un parser que no pudo leer la fecha.
- "Recurrencia" = nº de conversaciones distintas del cliente en el proyecto.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, Evaluation, Message
from app.models.reputation import UserReputation
from app.services.reputation import hash_user_key

logger = logging.getLogger(__name__)

# Timestamps anteriores a esto son el fallback epoch de un parser roto → ruido.
_EPOCH_CUTOFF = datetime(1971, 1, 1, tzinfo=UTC)


def _respectful(rep: UserReputation | None) -> bool:
    if rep is None:
        return True
    return not (
        rep.usuario_riesgoso
        or (rep.fraud_attempts or 0) > 0
        or rep.etiqueta == "problematico"
    )


async def _reputation_by_key(
    session: AsyncSession, project_id: uuid.UUID
) -> dict[str, UserReputation]:
    reps = (
        (
            await session.execute(
                select(UserReputation).where(UserReputation.project_id == project_id)
            )
        )
        .scalars()
        .all()
    )
    return {rep.user_key: rep for rep in reps}


async def list_clients(session: AsyncSession, project_id: uuid.UUID) -> list[dict]:
    """Un item por external_id del proyecto, con reputación + recurrencia + score."""
    rows = (
        await session.execute(
            select(
                Conversation.external_id,
                Conversation.contact_name,
                Conversation.started_at,
                Conversation.created_at,
                Evaluation.score,
                Evaluation.satisfaction,
                Evaluation.segment,
            )
            .outerjoin(Evaluation, Evaluation.conversation_id == Conversation.id)
            .where(Conversation.project_id == project_id)
        )
    ).all()

    rep_by_key = await _reputation_by_key(session, project_id)
    clients: dict[str, dict] = {}
    for r in rows:
        c = clients.setdefault(
            r.external_id,
            {
                "externalId": r.external_id,
                "contactName": r.contact_name,
                "conversations": 0,
                "scores": [],
                "sats": [],
                "segments": {},
                "lastSeen": None,
            },
        )
        c["conversations"] += 1
        if r.score is not None:
            c["scores"].append(r.score)
        if r.satisfaction is not None:
            c["sats"].append(r.satisfaction)
        if r.segment:
            c["segments"][r.segment] = c["segments"].get(r.segment, 0) + 1
        seen = r.started_at or r.created_at
        if seen and (c["lastSeen"] is None or seen > c["lastSeen"]):
            c["lastSeen"] = seen

    out = []
    for ext, c in clients.items():
        rep = rep_by_key.get(hash_user_key(ext))
        out.append(
            {
                "externalId": ext,
                "contactName": c["contactName"],
                "conversations": c["conversations"],
                "avgScore": (
                    round(sum(c["scores"]) / len(c["scores"])) if c["scores"] else None
                ),
                "avgSatisfaction": (
                    round(sum(c["sats"]) / len(c["sats"]), 1) if c["sats"] else None
                ),
                "etiqueta": rep.etiqueta if rep else "neutral",
                "respectful": _respectful(rep),
                "riesgoso": bool(rep.usuario_riesgoso) if rep else False,
                "fraudAttempts": (rep.fraud_attempts or 0) if rep else 0,
                "lastSeen": c["lastSeen"].isoformat() if c["lastSeen"] else None,
            }
        )
    # Los riesgosos / con peor score primero.
    out.sort(key=lambda x: (not x["riesgoso"], x["avgScore"] or 100))
    return out


async def response_hours(
    session: AsyncSession, project_id: uuid.UUID, external_id: str
) -> list[int]:
    """Histograma 0..23 (UTC) de mensajes del cliente (role='user')."""
    hour_expr = func.extract("hour", Message.timestamp)
    rows = (
        await session.execute(
            select(hour_expr.label("h"), func.count().label("n"))
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.project_id == project_id,
                Conversation.external_id == external_id,
                Message.role == "user",
                Message.timestamp >= _EPOCH_CUTOFF,
            )
            .group_by(hour_expr)
        )
    ).all()
    hist = [0] * 24
    for r in rows:
        if r.h is not None:
            hist[int(r.h) % 24] = int(r.n)
    return hist


async def get_client_profile(
    session: AsyncSession, project_id: uuid.UUID, external_id: str
) -> dict | None:
    """Perfil completo de un cliente (o None si no tiene conversaciones)."""
    rows = (
        await session.execute(
            select(
                Conversation.public_id,
                Conversation.contact_name,
                Conversation.contact_phone,
                Conversation.started_at,
                Conversation.preview,
                Evaluation.score,
                Evaluation.satisfaction,
                Evaluation.segment,
                Evaluation.topic,
                Evaluation.tone,
                Evaluation.frustration,
            )
            .outerjoin(Evaluation, Evaluation.conversation_id == Conversation.id)
            .where(
                Conversation.project_id == project_id,
                Conversation.external_id == external_id,
            )
        )
    ).all()
    if not rows:
        return None

    rep = (
        await session.execute(
            select(UserReputation).where(
                UserReputation.project_id == project_id,
                UserReputation.user_key == hash_user_key(external_id),
            )
        )
    ).scalar_one_or_none()

    scores = [r.score for r in rows if r.score is not None]
    sats = [r.satisfaction for r in rows if r.satisfaction is not None]
    topics: dict[str, int] = {}
    for r in rows:
        if r.topic:
            topics[r.topic] = topics.get(r.topic, 0) + 1
    try:
        hours = await response_hours(session, project_id, external_id)
    except Exception as exc:  # degradar: sin histograma, pero devolver el perfil
        logger.warning(
            "[CLIENTS] response_hours falló ext=%s: %s — sin histograma",
            external_id,
            exc,
        )
        hours = [0] * 24
    most_active = max(range(24), key=lambda h: hours[h]) if any(hours) else None

    return {
        "externalId": external_id,
        "contactName": rows[0].contact_name,
        "contactPhone": rows[0].contact_phone,
        "conversations": len(rows),
        "avgScore": round(sum(scores) / len(scores)) if scores else None,
        "avgSatisfaction": round(sum(sats) / len(sats), 1) if sats else None,
        "frustratedCount": sum(1 for r in rows if r.frustration),
        "etiqueta": rep.etiqueta if rep else "neutral",
        "respectful": _respectful(rep),
        "riesgoso": bool(rep.usuario_riesgoso) if rep else False,
        "fraudAttempts": (rep.fraud_attempts or 0) if rep else 0,
        "avgSentiment": (
            float(rep.avg_sentiment) if rep and rep.avg_sentiment is not None else None
        ),
        "topics": sorted(topics.items(), key=lambda x: x[1], reverse=True),
        "responseHoursUtc": hours,
        "mostActiveHourUtc": most_active,
    }
