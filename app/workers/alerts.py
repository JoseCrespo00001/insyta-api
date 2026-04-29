"""Alerts worker (EQUIP-95).

Invoked from `app.workers.evaluator` right after an Evaluation row is
persisted (see ADR 0005). For each evaluation we:

  1. Load `projects.alert_thresholds` (jsonb) — the migration 0005 default
     applies if the customer hasn't tuned it.
  2. For each alert type, decide whether the threshold trips for this
     evaluation. Some types need rolling aggregates (score_drop, new_topic,
     escalation), so we run a small SQL per type.
  3. Insert an Alert row with `INSERT ... ON CONFLICT (project_id, type,
     dedup_key, window_start) DO NOTHING RETURNING id`. The first writer wins
     and fires the notification; subsequent writers no-op (count is bumped
     atomically — see ADR 0003).
  4. On a winning insert, send an email via Resend if configured AND POST to
     the project's `alert_webhook_url` if configured. Failures are logged but
     do not roll back the alert row — the alert is the durable record.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import Integer, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import async_session_factory
from app.models import Alert, Evaluation, Project
from app.services.celery_app import celery_app

logger = logging.getLogger(__name__)


DEFAULT_THRESHOLDS: dict[str, dict[str, Any]] = {
    "frustration": {"score_below": 40},
    "score_drop": {"percent_drop": 20, "rolling_days": 7},
    "new_topic": {"min_conversations_24h": 5},
    "escalation": {"percent_above_24h": 10},
}

WINDOW_HOURS_BY_TYPE: dict[str, int] = {
    "frustration": 1,
    "score_drop": 24,
    "new_topic": 24,
    "escalation": 24,
}


def _floor_window(now: datetime, hours: int) -> datetime:
    """Floor `now` to the start of the current `hours`-wide bucket (UTC)."""
    epoch_h = int(now.timestamp() // 3600)
    floored_h = (epoch_h // hours) * hours
    return datetime.fromtimestamp(floored_h * 3600, tz=timezone.utc)


def _merge_thresholds(custom: dict | None) -> dict:
    out = {k: dict(v) for k, v in DEFAULT_THRESHOLDS.items()}
    if isinstance(custom, dict):
        for k, v in custom.items():
            if k in out and isinstance(v, dict):
                out[k].update(v)
    return out


async def _frustration(
    session: AsyncSession, ev: Evaluation, thresholds: dict
) -> tuple[str, dict] | None:
    if ev.score is None:
        return None
    if ev.score >= thresholds["frustration"]["score_below"]:
        return None
    return str(ev.conversation_id), {
        "score": ev.score,
        "conversation_id": str(ev.conversation_id),
    }


async def _score_drop(
    session: AsyncSession, ev: Evaluation, thresholds: dict
) -> tuple[str, dict] | None:
    cfg = thresholds["score_drop"]
    rolling_days = int(cfg.get("rolling_days", 7))
    pct = float(cfg.get("percent_drop", 20))
    cutoff = datetime.now(timezone.utc) - timedelta(days=rolling_days)
    result = await session.execute(
        select(func.avg(Evaluation.score)).where(
            Evaluation.agent_id == ev.agent_id,
            Evaluation.evaluated_at >= cutoff,
            Evaluation.score.isnot(None),
            Evaluation.id != ev.id,
        )
    )
    rolling_avg = result.scalar_one_or_none()
    if rolling_avg is None or ev.score is None:
        return None
    if rolling_avg <= 0:
        return None
    drop_pct = (float(rolling_avg) - float(ev.score)) / float(rolling_avg) * 100
    if drop_pct < pct:
        return None
    return str(ev.agent_id), {
        "rolling_avg": float(rolling_avg),
        "current_score": ev.score,
        "drop_pct": round(drop_pct, 2),
    }


async def _new_topic(
    session: AsyncSession, ev: Evaluation, thresholds: dict
) -> tuple[str, dict] | None:
    if not ev.topic:
        return None
    cfg = thresholds["new_topic"]
    min_count = int(cfg.get("min_conversations_24h", 5))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    result = await session.execute(
        select(func.count(Evaluation.id)).where(
            Evaluation.project_id == ev.project_id,
            Evaluation.topic == ev.topic,
            Evaluation.evaluated_at >= cutoff,
        )
    )
    count = int(result.scalar_one() or 0)
    if count < min_count:
        return None
    # Was this topic absent in the prior 7d window before the 24h surge?
    prior_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    prior_result = await session.execute(
        select(func.count(Evaluation.id)).where(
            Evaluation.project_id == ev.project_id,
            Evaluation.topic == ev.topic,
            Evaluation.evaluated_at >= prior_cutoff,
            Evaluation.evaluated_at < cutoff,
        )
    )
    prior = int(prior_result.scalar_one() or 0)
    if prior > 0:
        return None
    return ev.topic, {
        "topic": ev.topic,
        "count_24h": count,
    }


async def _escalation(
    session: AsyncSession, ev: Evaluation, thresholds: dict
) -> tuple[str, dict] | None:
    cfg = thresholds["escalation"]
    pct = float(cfg.get("percent_above_24h", 10))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    result = await session.execute(
        select(
            func.count(Evaluation.id),
            func.sum(func.cast(Evaluation.escalated, Integer)),
        ).where(
            Evaluation.agent_id == ev.agent_id,
            Evaluation.evaluated_at >= cutoff,
        )
    )
    total, escalated = result.one()
    total = int(total or 0)
    escalated = int(escalated or 0)
    if total < 5:
        return None
    rate = escalated / total * 100
    if rate < pct:
        return None
    return str(ev.agent_id), {
        "total_24h": total,
        "escalated_24h": escalated,
        "rate_pct": round(rate, 2),
    }


_DETECTORS = {
    "frustration": _frustration,
    "score_drop": _score_drop,
    "new_topic": _new_topic,
    "escalation": _escalation,
}


async def _try_insert_alert(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    org_id: uuid.UUID,
    type: str,
    dedup_key: str,
    payload: dict,
) -> uuid.UUID | None:
    window_hours = WINDOW_HOURS_BY_TYPE[type]
    window = _floor_window(datetime.now(timezone.utc), window_hours)
    public_id = f"alt_{uuid.uuid4().hex[:24]}"
    stmt = (
        pg_insert(Alert)
        .values(
            id=uuid.uuid4(),
            public_id=public_id,
            project_id=project_id,
            org_id=org_id,
            type=type,
            dedup_key=dedup_key,
            window_start=window,
            payload=payload,
            count=1,
        )
        .on_conflict_do_nothing(
            index_elements=[
                "project_id",
                "type",
                "dedup_key",
                "window_start",
            ],
        )
        .returning(Alert.id)
    )
    result = await session.execute(stmt)
    inserted = result.scalar_one_or_none()
    if inserted is None:
        # Bump the count on the existing dedup row.
        await session.execute(
            update(Alert)
            .where(
                Alert.project_id == project_id,
                Alert.type == type,
                Alert.dedup_key == dedup_key,
                Alert.window_start == window,
            )
            .values(count=Alert.count + 1)
        )
    return inserted


def _send_email(to: str, subject: str, body: str) -> bool:
    settings = get_settings()
    api_key = settings.resend_api_key
    if not api_key or api_key.startswith("re_..."):
        logger.info("[ALERTS_EMAIL] Resend not configured — skipping email to %s", to)
        return False
    try:
        r = httpx.post(
            "https://api.resend.com/emails",
            json={
                "from": settings.email_from,
                "to": [to],
                "subject": subject,
                "text": body,
            },
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5.0,
        )
        r.raise_for_status()
        return True
    except Exception as exc:
        logger.warning("[ALERTS_EMAIL] Resend send failed: %s", exc)
        return False


def _send_webhook(url: str, payload: dict) -> bool:
    try:
        r = httpx.post(url, json=payload, timeout=5.0)
        r.raise_for_status()
        return True
    except Exception as exc:
        logger.warning("[ALERTS_WEBHOOK] POST %s failed: %s", url, exc)
        return False


async def _notify(
    session: AsyncSession,
    *,
    alert_id: uuid.UUID,
    project_id: uuid.UUID,
    type: str,
    payload: dict,
) -> None:
    proj_row = await session.execute(
        text(
            "SELECT public_id, org_id, name, alert_webhook_url "
            "FROM projects WHERE id = :id"
        ),
        {"id": project_id},
    )
    project = proj_row.one_or_none()
    if project is None:
        return
    via: list[str] = []
    org_email_result = await session.execute(
        text(
            "SELECT u.email FROM users u "
            "WHERE u.org_id = :oid AND u.role IN ('owner','admin') "
            "ORDER BY u.created_at LIMIT 1"
        ),
        {"oid": project.org_id},
    )
    owner_email = org_email_result.scalar_one_or_none()
    if owner_email:
        if _send_email(
            owner_email,
            subject=f"[Insyta] {type} alert — {project.name}",
            body=f"Alert payload: {payload}",
        ):
            via.append("email")
    if project.alert_webhook_url:
        if _send_webhook(
            project.alert_webhook_url,
            {
                "type": type,
                "project_public_id": project.public_id,
                "payload": payload,
            },
        ):
            via.append("webhook")
    if via:
        await session.execute(
            update(Alert)
            .where(Alert.id == alert_id)
            .values(notified_at=datetime.now(timezone.utc), notified_via=",".join(via))
        )


async def _dispatch(evaluation_id: uuid.UUID) -> dict:
    fired: list[str] = []
    async with async_session_factory() as session:
        ev = await session.get(Evaluation, evaluation_id)
        if ev is None:
            return {"evaluation_id": str(evaluation_id), "skipped": True}
        project = await session.get(Project, ev.project_id)
        thresholds = _merge_thresholds(project.alert_thresholds if project else None)

        for atype, detector in _DETECTORS.items():
            try:
                hit = await detector(session, ev, thresholds)
            except Exception as exc:
                logger.exception(
                    "[ALERTS] detector %s crashed on eval=%s: %s",
                    atype,
                    evaluation_id,
                    exc,
                )
                continue
            if hit is None:
                continue
            dedup_key, payload = hit
            inserted_id = await _try_insert_alert(
                session,
                project_id=ev.project_id,
                org_id=ev.org_id,
                type=atype,
                dedup_key=dedup_key,
                payload=payload,
            )
            if inserted_id is not None:
                await session.commit()
                await _notify(
                    session,
                    alert_id=inserted_id,
                    project_id=ev.project_id,
                    type=atype,
                    payload=payload,
                )
                fired.append(atype)
        await session.commit()

    logger.info(
        "[ALERTS] eval=%s fired=%s",
        evaluation_id,
        fired,
    )
    return {"evaluation_id": str(evaluation_id), "fired": fired}


@celery_app.task(name="app.workers.alerts.dispatch_alerts", bind=True)
def dispatch_alerts(self, evaluation_id: str) -> dict:
    return asyncio.run(_dispatch(uuid.UUID(evaluation_id)))
