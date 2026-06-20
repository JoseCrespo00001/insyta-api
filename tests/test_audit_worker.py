"""Worker test for run_audit with a mocked LLM (no API key needed).

Seeds a conversation, stubs the conversation-level judge (LLMRouter) and the
per-message judge (judge_messages), runs the worker, and asserts that
evaluations + message_evaluations + the audit report are produced.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.llm.audit_judge import MessageVerdict
from app.llm.schemas import EvaluationResponse, LLMUsage
from app.workers import audit as audit_worker

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/insyta",
)


async def _db_available() -> bool:
    eng = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with eng.connect() as c:
            await c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        await eng.dispose()


class _FakeRouter:
    async def evaluate(self, messages):
        parsed = EvaluationResponse(
            score=42,
            resolution=False,
            satisfaction=2,
            tone="negative",
            frustration=True,
            escalated=False,
            efficiency=2,
            scope_violation=True,
            topic="devoluciones",
            summary="El agente inventó una política inexistente.",
        )
        usage = LLMUsage(
            input_tokens=100,
            output_tokens=20,
            cost_usd=Decimal("0.0001"),
            model="mock",
            latency_ms=5,
        )
        return parsed, usage


@pytest.mark.asyncio
async def test_run_audit_produces_eval_and_message_evals(monkeypatch):
    if not await _db_available():
        pytest.skip("DB unavailable")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

    # Stub both judges so no API key is needed.
    monkeypatch.setattr(audit_worker, "LLMRouter", lambda: _FakeRouter())

    async def _fake_judge(messages, *, emphasis=None, free_text=None):
        return [
            MessageVerdict(
                seq=1,
                label="error",
                issue_type="alucinacion",
                issue_subtype="promo_inexistente",
                severity="alta",
                note="Inventó un código de descuento.",
            )
        ]

    monkeypatch.setattr(audit_worker, "judge_messages", _fake_judge)

    eng = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(eng, expire_on_commit=False)
    org_id = uuid.uuid4()
    proj_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    conv_id = uuid.uuid4()
    audit_id = uuid.uuid4()
    try:
        async with factory() as s, s.begin():
            await s.execute(
                text(
                    "INSERT INTO organizations(id, public_id, slug, name) "
                    "VALUES (:id, :pid, :slug, 'Org')"
                ),
                {
                    "id": org_id,
                    "pid": f"org_{org_id.hex[:16]}",
                    "slug": f"o-{org_id.hex[:6]}",
                },
            )
            await s.execute(
                text(
                    "INSERT INTO projects(id, public_id, org_id, slug, name) "
                    "VALUES (:id, :pid, :org, :slug, 'P')"
                ),
                {
                    "id": proj_id,
                    "pid": f"proj_{proj_id.hex[:16]}",
                    "org": org_id,
                    "slug": f"p-{proj_id.hex[:6]}",
                },
            )
            await s.execute(
                text(
                    "INSERT INTO agents(id, public_id, project_id, org_id, slug, name, platform) "
                    "VALUES (:id, :pid, :proj, :org, 'default', 'A', 'custom_sdk')"
                ),
                {
                    "id": agent_id,
                    "pid": f"agt_{agent_id.hex[:16]}",
                    "proj": proj_id,
                    "org": org_id,
                },
            )
            await s.execute(
                text(
                    "INSERT INTO conversations(id, public_id, project_id, org_id, agent_id, "
                    "external_id, platform, message_count, status) "
                    "VALUES (:id, :pid, :proj, :org, :agent, 'ext-1', 'custom_sdk', 2, 'completed')"
                ),
                {
                    "id": conv_id,
                    "pid": f"conv_{conv_id.hex[:16]}",
                    "proj": proj_id,
                    "org": org_id,
                    "agent": agent_id,
                },
            )
            for seq, role, content in [
                (0, "user", "hola"),
                (1, "assistant", "usá BIENVENIDO25"),
            ]:
                await s.execute(
                    text(
                        "INSERT INTO messages(id, public_id, conversation_id, project_id, org_id, "
                        "seq, role, content, timestamp) "
                        "VALUES (:id, :pid, :conv, :proj, :org, :seq, :role, :content, now())"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "pid": f"msg_{uuid.uuid4().hex[:16]}",
                        "conv": conv_id,
                        "proj": proj_id,
                        "org": org_id,
                        "seq": seq,
                        "role": role,
                        "content": content,
                    },
                )
            await s.execute(
                text(
                    "INSERT INTO audits(id, public_id, project_id, org_id, name, status, conversation_count) "
                    "VALUES (:id, :pid, :proj, :org, 'Audit', 'running', 1)"
                ),
                {
                    "id": audit_id,
                    "pid": f"aud_{audit_id.hex[:16]}",
                    "proj": proj_id,
                    "org": org_id,
                },
            )
            await s.execute(
                text(
                    "INSERT INTO audit_conversations(id, audit_id, conversation_id, project_id, org_id) "
                    "VALUES (:id, :aud, :conv, :proj, :org)"
                ),
                {
                    "id": uuid.uuid4(),
                    "aud": audit_id,
                    "conv": conv_id,
                    "proj": proj_id,
                    "org": org_id,
                },
            )

        result = await audit_worker._run(audit_id, org_id)
        assert result["evaluated"] == 1
        assert result["message_evaluations"] == 1

        async with factory() as s:
            ev = (
                await s.execute(
                    text(
                        "SELECT score, resolution FROM evaluations WHERE conversation_id = :c"
                    ),
                    {"c": conv_id},
                )
            ).one()
            assert ev.score == 42
            assert ev.resolution is False

            me = (
                await s.execute(
                    text(
                        "SELECT issue_type, severity FROM message_evaluations WHERE audit_id = :a"
                    ),
                    {"a": audit_id},
                )
            ).one()
            assert me.issue_type == "alucinacion"
            assert me.severity == "alta"

            au = (
                await s.execute(
                    text(
                        "SELECT status, report_summary, suggestions FROM audits WHERE id = :a"
                    ),
                    {"a": audit_id},
                )
            ).one()
            assert au.status == "active"
            assert au.report_summary["total"] == 1
            assert len(au.suggestions) >= 1
    finally:
        async with factory() as s, s.begin():
            await s.execute(
                text("DELETE FROM organizations WHERE id = :id"), {"id": org_id}
            )
        await eng.dispose()
