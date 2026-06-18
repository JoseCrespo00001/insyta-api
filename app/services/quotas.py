"""Stub quota check.

Sprint 1 only enforces a coarse "free tier = 1000 conversations/month" cap as a
placeholder. The Stripe-backed implementation lands in Sprint 4 (EQUIP-105).
For now the function returns whether the upload would exceed the cap given the
current month's processed-rows count.

Override: set `QUOTA_OVERRIDE_PROJECTS=pyme_maria,other_project` (CSV of
project public_ids) to bypass the cap entirely for those projects. Intended for
the Optimization Loop CLI and synthetic dataset generation during TP4 research.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Upload

logger = logging.getLogger(__name__)

FREE_PLAN_MONTHLY_CAP = 1000

# CSV of project_public_id values that skip quota checks. Read once at module
# load; requires process restart to pick up changes. Intentionally simple — no
# hot-reload — because this is a research/TFG escape hatch, not a product feature.
_OVERRIDE_PROJECTS: frozenset[str] = frozenset(
    p.strip()
    for p in os.environ.get("QUOTA_OVERRIDE_PROJECTS", "").split(",")
    if p.strip()
)


async def upload_would_exceed_quota(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    new_rows: int,
    project_public_id: str | None = None,
) -> bool:
    """Returns True if accepting `new_rows` more rows this month would exceed the cap.

    If `project_public_id` is in QUOTA_OVERRIDE_PROJECTS the check is skipped
    and this function always returns False (i.e., allowed).
    """
    if project_public_id and project_public_id in _OVERRIDE_PROJECTS:
        logger.warning(
            "[QUOTAS] quota check bypassed for project_public_id=%s "
            "(QUOTA_OVERRIDE_PROJECTS override active)",
            project_public_id,
        )
        return False

    now = datetime.now(timezone.utc)
    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    result = await session.execute(
        select(func.coalesce(func.sum(Upload.rows_processed), 0)).where(
            Upload.org_id == org_id,
            Upload.created_at >= start_of_month,
            Upload.status == "completed",
        )
    )
    used = int(result.scalar_one() or 0)
    remaining = FREE_PLAN_MONTHLY_CAP - used
    return new_rows > remaining
