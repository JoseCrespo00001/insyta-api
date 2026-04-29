"""projects.alert_thresholds default values

Revision ID: 0005_alert_thresholds
Revises: 0004_webhook_events
Create Date: 2026-04-28 20:10:00.000000

The column already exists on `projects` (added in 0001_initial). This
migration backfills a sensible default for tenants that have not customized
thresholds. Format mirrors the keys consumed by `app/workers/alerts.py`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from alembic import op

revision: str = "0005_alert_thresholds"
down_revision: str | Sequence[str] | None = "0004_webhook_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_THRESHOLDS = {
    "frustration": {"score_below": 40},
    "score_drop": {"percent_drop": 20, "rolling_days": 7},
    "new_topic": {"min_conversations_24h": 5},
    "escalation": {"percent_above_24h": 10},
}


def upgrade() -> None:
    op.execute(
        f"UPDATE projects SET alert_thresholds = '{json.dumps(DEFAULT_THRESHOLDS)}'::jsonb "
        f"WHERE alert_thresholds IS NULL"
    )


def downgrade() -> None:
    # No-op: we do not unset thresholds on downgrade.
    pass
