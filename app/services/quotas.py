"""Stub quota check.

Sprint 1 only enforces a coarse "free tier = 1000 conversations/month" cap as a
placeholder. The Stripe-backed implementation lands in Sprint 4 (EQUIP-105).
For now the function returns whether the upload would exceed the cap given the
current month's processed-rows count.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Upload

logger = logging.getLogger(__name__)

FREE_PLAN_MONTHLY_CAP = 1000


async def upload_would_exceed_quota(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    new_rows: int,
) -> bool:
    """Returns True if accepting `new_rows` more rows this month would exceed the cap."""
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
