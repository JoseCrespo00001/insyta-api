"""DB-backed tests for the alerts worker (EQUIP-95)."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

_FERNET_KEY = Fernet.generate_key().decode()
os.environ.setdefault("WEBHOOK_SECRET_KEY", _FERNET_KEY)

from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.core.db import async_session_factory  # noqa: E402
from app.workers import alerts as alerts_module  # noqa: E402

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/insyta",
)


async def _db_available() -> bool:
    try:
        async with async_session_factory() as s:
            await s.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _seed() -> dict:
    org_id = uuid.uuid4()
    proj_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    test_ws = Fernet(Fernet.generate_key()).encrypt(b"unused")
    async with async_session_factory() as s:
        async with s.begin():
            await s.execute(
                text(
                    "INSERT INTO organizations(id, public_id, slug, name) "
                    "VALUES (:id, :pid, :slug, 'Org')"
                ),
                {
                    "id": org_id,
                    "pid": f"org_{org_id.hex[:16]}",
                    "slug": f"org-{org_id.hex[:6]}",
                },
            )
            await s.execute(
                text(
                    "INSERT INTO projects"
                    "(id, public_id, org_id, slug, name, webhook_secret_encrypted) "
                    "VALUES (:id, :pid, :org, 'p', 'P', :ws)"
                ),
                {
                    "id": proj_id,
                    "pid": f"proj_{proj_id.hex[:16]}",
                    "org": org_id,
                    "ws": test_ws,
                },
            )
            await s.execute(
                text(
                    "INSERT INTO agents"
                    "(id, public_id, project_id, org_id, slug, name, platform) "
                    "VALUES (:id, :pid, :proj, :org, 'a', 'A', 'wati')"
                ),
                {
                    "id": agent_id,
                    "pid": f"agt_{agent_id.hex[:16]}",
                    "proj": proj_id,
                    "org": org_id,
                },
            )
    return {"org_id": org_id, "proj_id": proj_id, "agent_id": agent_id}


async def _cleanup_org(org_id: uuid.UUID) -> None:
    async with async_session_factory() as s:
        async with s.begin():
            await s.execute(
                text("DELETE FROM organizations WHERE id = :id"), {"id": org_id}
            )


async def _insert_eval(
    *,
    project_id: uuid.UUID,
    org_id: uuid.UUID,
    agent_id: uuid.UUID,
    score: int,
    topic: str = "general",
    escalated: bool = False,
    evaluated_at: datetime | None = None,
) -> uuid.UUID:
    """Inserts a conversation + evaluation pair for the test."""
    conv_id = uuid.uuid4()
    eval_id = uuid.uuid4()
    when = evaluated_at or datetime.now(timezone.utc)
    async with async_session_factory() as s:
        async with s.begin():
            await s.execute(
                text(
                    "INSERT INTO conversations"
                    "(id, public_id, project_id, org_id, agent_id, external_id, platform, status) "
                    "VALUES (:id, :pid, :proj, :org, :agent, :ext, 'wati', 'completed')"
                ),
                {
                    "id": conv_id,
                    "pid": f"conv_{conv_id.hex[:16]}",
                    "proj": project_id,
                    "org": org_id,
                    "agent": agent_id,
                    "ext": f"ext-{conv_id.hex[:8]}",
                },
            )
            await s.execute(
                text(
                    "INSERT INTO evaluations"
                    "(id, public_id, conversation_id, project_id, org_id, agent_id, "
                    " score, topic, escalated, evaluated_at) "
                    "VALUES (:id, :pid, :conv, :proj, :org, :agent, "
                    "        :score, :topic, :esc, :when)"
                ),
                {
                    "id": eval_id,
                    "pid": f"eval_{eval_id.hex[:16]}",
                    "conv": conv_id,
                    "proj": project_id,
                    "org": org_id,
                    "agent": agent_id,
                    "score": score,
                    "topic": topic,
                    "esc": escalated,
                    "when": when,
                },
            )
    return eval_id


@pytest.fixture(autouse=True)
def _stub_notifications(monkeypatch):
    """Avoid hitting Resend / external webhooks during tests."""
    monkeypatch.setattr(alerts_module, "_send_email", lambda *a, **k: True)
    monkeypatch.setattr(alerts_module, "_send_webhook", lambda *a, **k: True)


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.core.db import engine

    await engine.dispose()


@pytest.mark.asyncio
async def test_low_score_fires_frustration_alert():
    if not await _db_available():
        pytest.skip("PostgreSQL test DB unavailable")
    ids = await _seed()
    try:
        eid = await _insert_eval(
            project_id=ids["proj_id"],
            org_id=ids["org_id"],
            agent_id=ids["agent_id"],
            score=20,
        )
        result = await alerts_module._dispatch(eid)
        assert "frustration" in result["fired"]

        async with async_session_factory() as s:
            row = await s.execute(
                text(
                    "SELECT count(*) FROM alerts "
                    "WHERE project_id = :p AND type = 'frustration'"
                ),
                {"p": ids["proj_id"]},
            )
            assert row.scalar_one() == 1
    finally:
        await _cleanup_org(ids["org_id"])


@pytest.mark.asyncio
async def test_high_score_does_not_fire_frustration():
    if not await _db_available():
        pytest.skip("PostgreSQL test DB unavailable")
    ids = await _seed()
    try:
        eid = await _insert_eval(
            project_id=ids["proj_id"],
            org_id=ids["org_id"],
            agent_id=ids["agent_id"],
            score=85,
        )
        result = await alerts_module._dispatch(eid)
        assert "frustration" not in result["fired"]
    finally:
        await _cleanup_org(ids["org_id"])


@pytest.mark.asyncio
async def test_dedup_keeps_one_alert_per_window():
    if not await _db_available():
        pytest.skip("PostgreSQL test DB unavailable")
    ids = await _seed()
    try:
        # Two evals on the SAME conversation_id -> dedup_key collides.
        conv_id = uuid.uuid4()
        async with async_session_factory() as s:
            async with s.begin():
                await s.execute(
                    text(
                        "INSERT INTO conversations"
                        "(id, public_id, project_id, org_id, agent_id, external_id, platform, status) "
                        "VALUES (:id, :pid, :proj, :org, :agent, :ext, 'wati', 'completed')"
                    ),
                    {
                        "id": conv_id,
                        "pid": f"conv_{conv_id.hex[:16]}",
                        "proj": ids["proj_id"],
                        "org": ids["org_id"],
                        "agent": ids["agent_id"],
                        "ext": "shared",
                    },
                )
        # Same conv -> same dedup_key for frustration.
        # The unique constraint is on the evaluations.conversation_id, so we
        # cannot insert two evals for the same conv. Use two convs but pass
        # the same dedup_key by hand via two _try_insert_alert calls.
        from app.workers.alerts import _try_insert_alert

        async with async_session_factory() as s:
            id1 = await _try_insert_alert(
                s,
                project_id=ids["proj_id"],
                org_id=ids["org_id"],
                type="frustration",
                dedup_key=str(conv_id),
                payload={"score": 10, "conversation_id": str(conv_id)},
            )
            await s.commit()
            id2 = await _try_insert_alert(
                s,
                project_id=ids["proj_id"],
                org_id=ids["org_id"],
                type="frustration",
                dedup_key=str(conv_id),
                payload={"score": 10, "conversation_id": str(conv_id)},
            )
            await s.commit()
        assert id1 is not None
        assert id2 is None

        async with async_session_factory() as s:
            row = await s.execute(
                text(
                    "SELECT count(*), max(count) FROM alerts "
                    "WHERE project_id = :p AND type = 'frustration' "
                    "AND dedup_key = :d"
                ),
                {"p": ids["proj_id"], "d": str(conv_id)},
            )
            count, bumped = row.one()
            assert count == 1
            assert bumped == 2
    finally:
        await _cleanup_org(ids["org_id"])


@pytest.mark.asyncio
async def test_score_drop_fires_when_rolling_avg_collapses():
    if not await _db_available():
        pytest.skip("PostgreSQL test DB unavailable")
    ids = await _seed()
    try:
        # 7 prior evals with avg=80
        for i in range(7):
            await _insert_eval(
                project_id=ids["proj_id"],
                org_id=ids["org_id"],
                agent_id=ids["agent_id"],
                score=80,
                evaluated_at=datetime.now(timezone.utc) - timedelta(days=1, hours=i),
            )
        # New eval at 30 -> 62.5% drop, way over 20%
        eid = await _insert_eval(
            project_id=ids["proj_id"],
            org_id=ids["org_id"],
            agent_id=ids["agent_id"],
            score=30,
        )
        result = await alerts_module._dispatch(eid)
        assert "score_drop" in result["fired"]
        # Frustration also trips since 30 < 40.
        assert "frustration" in result["fired"]
    finally:
        await _cleanup_org(ids["org_id"])


@pytest.mark.asyncio
async def test_new_topic_fires_when_threshold_crossed():
    if not await _db_available():
        pytest.skip("PostgreSQL test DB unavailable")
    ids = await _seed()
    try:
        # Insert 5 evals on topic "shipping" within 24h, none before.
        for _ in range(4):
            await _insert_eval(
                project_id=ids["proj_id"],
                org_id=ids["org_id"],
                agent_id=ids["agent_id"],
                score=70,
                topic="shipping",
            )
        eid = await _insert_eval(
            project_id=ids["proj_id"],
            org_id=ids["org_id"],
            agent_id=ids["agent_id"],
            score=70,
            topic="shipping",
        )
        result = await alerts_module._dispatch(eid)
        assert "new_topic" in result["fired"]
    finally:
        await _cleanup_org(ids["org_id"])


@pytest.mark.asyncio
async def test_escalation_fires_when_rate_above_threshold():
    if not await _db_available():
        pytest.skip("PostgreSQL test DB unavailable")
    ids = await _seed()
    try:
        # 5 evals in 24h, 2 escalated => 40% > 10%.
        for esc in [True, True, False, False, False]:
            await _insert_eval(
                project_id=ids["proj_id"],
                org_id=ids["org_id"],
                agent_id=ids["agent_id"],
                score=70,
                escalated=esc,
            )
        # The trigger eval (latest) — escalated=False, score 70.
        eid = await _insert_eval(
            project_id=ids["proj_id"],
            org_id=ids["org_id"],
            agent_id=ids["agent_id"],
            score=70,
            escalated=False,
        )
        result = await alerts_module._dispatch(eid)
        assert "escalation" in result["fired"]
    finally:
        await _cleanup_org(ids["org_id"])


@pytest.mark.asyncio
async def test_50_evals_with_varied_scores_yields_consistent_alerts():
    """Spec sanity: ~half low-score evals trigger frustration once each (one
    per conversation, dedup keeps it to one within the 1h window)."""
    if not await _db_available():
        pytest.skip("PostgreSQL test DB unavailable")
    ids = await _seed()
    try:
        eval_ids: list[uuid.UUID] = []
        for i in range(50):
            score = 25 if i % 2 == 0 else 90
            eid = await _insert_eval(
                project_id=ids["proj_id"],
                org_id=ids["org_id"],
                agent_id=ids["agent_id"],
                score=score,
            )
            eval_ids.append(eid)
        for eid in eval_ids:
            await alerts_module._dispatch(eid)

        async with async_session_factory() as s:
            row = await s.execute(
                text(
                    "SELECT count(*) FROM alerts "
                    "WHERE project_id = :p AND type = 'frustration'"
                ),
                {"p": ids["proj_id"]},
            )
            n = row.scalar_one()
        # 25 low-score evals on 25 distinct conversations -> 25 frustration
        # alerts (each with its own dedup_key).
        assert n == 25
    finally:
        await _cleanup_org(ids["org_id"])
