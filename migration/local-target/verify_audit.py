"""Verificación #3 del loop de migración: una auditoría corre end-to-end contra el
Postgres LOCAL del stack destino (docker-compose migration/local-target), con el judge
stubbeado (sin LLM, sin API key) y SIN pytest/conftest.

Prueba que `run_audit._run` escribe evaluations + message_evaluations + report_summary
en el Postgres nuevo — o sea, la pieza que cambia en la migración (host de la DB) no
rompe el pipeline de auditoría.

Correr con el stack local arriba:
    uv run python migration/local-target/verify_audit.py
"""

import os

# El engine de la app se construye al importar app.core.db → fijamos la DB ANTES.
os.environ["DATABASE_URL"] = "postgresql+asyncpg://postgres:postgres@localhost:5544/insyta_target"
os.environ["MIGRATION_DATABASE_URL"] = os.environ["DATABASE_URL"]
os.environ["ENVIRONMENT"] = "development"

import asyncio  # noqa: E402
import uuid  # noqa: E402
from decimal import Decimal  # noqa: E402

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.llm.audit_judge import MessageVerdict  # noqa: E402
from app.llm.schemas import EvaluationResponse, LLMUsage  # noqa: E402
from app.workers import audit as audit_worker  # noqa: E402

DB = os.environ["DATABASE_URL"]


class _FakeRouter:
    async def evaluate(self, messages, *, context=None, **kwargs):
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
            summary="mock",
        )
        usage = LLMUsage(
            input_tokens=100,
            output_tokens=20,
            cost_usd=Decimal("0.0001"),
            model="mock",
            latency_ms=5,
        )
        return parsed, usage


async def _fake_judge(messages, *, emphasis=None, free_text=None, **kwargs):
    return [
        MessageVerdict(
            seq=1,
            label="error",
            issue_type="alucinacion",
            severity="alta",
            note="Inventó un código de descuento.",
        )
    ]


async def _fake_flowless(stats, examples_by_issue, **kwargs):
    return []


# Stubs (sin LLM). `_run` usa build_router(provider), no LLMRouter directo.
audit_worker.build_router = lambda provider=None: _FakeRouter()
audit_worker.judge_messages = _fake_judge
audit_worker.generate_flowless_suggestions = _fake_flowless


async def main() -> None:
    eng = create_async_engine(DB, pool_pre_ping=True)
    factory = async_sessionmaker(eng, expire_on_commit=False)
    org_id = uuid.uuid4()
    proj_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    conv_id = uuid.uuid4()
    audit_id = uuid.uuid4()

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
    assert result["evaluated"] == 1, result
    assert result["message_evaluations"] == 1, result

    async with factory() as s:
        ev = (
            await s.execute(
                text("SELECT score, resolution FROM evaluations WHERE conversation_id = :c"),
                {"c": conv_id},
            )
        ).one()
        assert ev.score == 42
        au = (
            await s.execute(
                text("SELECT status, report_summary FROM audits WHERE id = :a"),
                {"a": audit_id},
            )
        ).one()
        assert au.status == "active", au.status

    print(
        "PASS · auditoría end-to-end contra el Postgres LOCAL: "
        f"evaluated={result['evaluated']} msg_evals={result['message_evaluations']} "
        f"score={ev.score} status={au.status} "
        f"report_summary_keys={sorted((au.report_summary or {}).keys())}"
    )

    await eng.dispose()
    await audit_worker.engine.dispose()


asyncio.run(main())
