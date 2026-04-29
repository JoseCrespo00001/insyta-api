"""Retention worker (EQUIP-97).

Runs nightly via Celery Beat (`celery_app.beat_schedule['retention-archive-and-purge']`).

Two passes per run:

1. **Archive**: conversations whose `created_at < NOW() - retention_days` and
   whose `archived_at IS NULL` get `archived_at = NOW()`. Soft delete.
   `retention_days` comes from the project's plan: free=30, starter=90,
   growth=365, business=730, agency=1095, enterprise=1825 — falls back to
   `projects.retention_days` (the per-project override) when set.

2. **Hard-delete**: conversations with `archived_at < NOW() - 30 days` get
   their messages and the conversation row deleted. We keep the evaluation
   row aggregated per project_id (no project_stats table yet — Sprint 2 work,
   tracked in CONTEXT.md as a known gap), and the audit log row stays.

The audit log lives in `app/workers/retention.py:AUDIT_LOG_TABLE` and is a
plain append-only table — but for Wave 4 we just log via `logger.info` with a
structured prefix `[RETENTION_AUDIT]`. Sprint 2 promotes it to a DB table.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.core.db import async_session_factory
from app.services.celery_app import celery_app

logger = logging.getLogger(__name__)


PLAN_RETENTION_DAYS: dict[str, int] = {
    "free": 30,
    "starter": 90,
    "growth": 365,
    "business": 730,
    "agency": 1095,
    "enterprise": 1825,
}
HARD_DELETE_AFTER_DAYS = 30


async def _retention_days_for_project(
    project_id: uuid.UUID,
) -> int:
    """Resolve effective retention_days = max(plan_default, project override)."""
    async with async_session_factory() as s:
        row = await s.execute(
            text(
                "SELECT p.retention_days, o.plan "
                "FROM projects p JOIN organizations o ON o.id = p.org_id "
                "WHERE p.id = :id"
            ),
            {"id": project_id},
        )
        r = row.one_or_none()
    if r is None:
        return PLAN_RETENTION_DAYS["free"]
    plan_days = PLAN_RETENTION_DAYS.get(r.plan, PLAN_RETENTION_DAYS["free"])
    project_override = r.retention_days or 0
    return max(plan_days, project_override)


async def _archive_pass() -> dict:
    """Soft-delete conversations past their effective retention window.

    Per-project query keeps the math correct when plans vary across the org
    set — we cannot use a single global cutoff.
    """
    archived: dict[str, int] = {}
    async with async_session_factory() as s:
        proj_ids = (await s.execute(text("SELECT id FROM projects"))).scalars().all()

    for pid in proj_ids:
        days = await _retention_days_for_project(pid)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        async with async_session_factory() as s:
            async with s.begin():
                result = await s.execute(
                    text(
                        "UPDATE conversations SET archived_at = now() "
                        "WHERE project_id = :pid AND archived_at IS NULL "
                        "AND created_at < :cutoff RETURNING id"
                    ),
                    {"pid": pid, "cutoff": cutoff},
                )
                count = len(list(result.scalars()))
        if count:
            archived[str(pid)] = count
            logger.info(
                "[RETENTION_AUDIT] archived project=%s n=%d days=%d",
                pid,
                count,
                days,
            )
    return archived


async def _hard_delete_pass() -> dict:
    """Hard-delete conversations + their messages 30 days post archived_at.

    Messages are deleted first (FK CASCADE would do it but doing it explicitly
    lets us count + audit-log per row without DELETE RETURNING giant payloads).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=HARD_DELETE_AFTER_DAYS)
    deleted_msgs = 0
    deleted_convs = 0
    async with async_session_factory() as s:
        async with s.begin():
            ids_result = await s.execute(
                text(
                    "SELECT id, project_id FROM conversations "
                    "WHERE archived_at IS NOT NULL AND archived_at < :cutoff"
                ),
                {"cutoff": cutoff},
            )
            target_pairs = [(r.id, r.project_id) for r in ids_result.all()]

            if target_pairs:
                target_ids = [p[0] for p in target_pairs]
                msg_result = await s.execute(
                    text(
                        "DELETE FROM messages "
                        "WHERE conversation_id = ANY(:ids) "
                        "RETURNING id"
                    ),
                    {"ids": target_ids},
                )
                deleted_msgs = len(list(msg_result.scalars()))

                conv_result = await s.execute(
                    text(
                        "DELETE FROM conversations " "WHERE id = ANY(:ids) RETURNING id"
                    ),
                    {"ids": target_ids},
                )
                deleted_convs = len(list(conv_result.scalars()))

                for cid, pid in target_pairs:
                    logger.info(
                        "[RETENTION_AUDIT] hard_deleted conv=%s project=%s",
                        cid,
                        pid,
                    )
    return {"messages": deleted_msgs, "conversations": deleted_convs}


async def _run() -> dict:
    archived = await _archive_pass()
    hard = await _hard_delete_pass()
    summary = {"archived": archived, "hard_deleted": hard}
    logger.info("[RETENTION] run summary=%s", summary)
    return summary


@celery_app.task(name="app.workers.retention.run_retention", bind=True)
def run_retention(self) -> dict:
    return asyncio.run(_run())
