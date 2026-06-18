"""Integration tests for the Optimization Loop service (EQUIP TP4).

Runs without a real Anthropic key — the Sonnet client is stubbed via
unittest.mock. Requires the PostgreSQL test DB on port 5433 (same as other
integration tests); skips gracefully if unavailable.

Setup:
  - 1 org + 1 project + 4 agents (one per sub-agent slug).
  - 12 evaluations: 3 per sub-agent, all score < 50, all with failure pattern.

Asserts:
  - 4 patterns persisted (one per sub-agent, >=1 pattern each).
  - sonnet_cost_usd > 0.
  - output.sub_agents_analyzed == 4.
  - Each persisted Improvement row exists in DB with status='pending'.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.services.optimization_loop import (
    OptimizationLoopInput,
    run_optimization_loop,
)

# ---------------------------------------------------------------------------
# Canned Sonnet response (one pattern per call)
# ---------------------------------------------------------------------------
_CANNED_PATTERN = {
    "patterns": [
        {
            "pattern": "deriva a soporte sin intentar resolver",
            "root_cause": (
                "El prompt no especifica que el agente debe agotar las "
                "opciones propias antes de escalar."
            ),
            "conv_examples": ["conv_abc123", "conv_def456"],
            "sample_excerpts": [
                "Usuario: quiero devolver el producto. Agente: contacte a soporte.",
                "Usuario: mi pago fallo. Agente: llame al 0800.",
            ],
            "prompt_before": "Derivar al equipo de soporte cuando haya dudas.",
            "prompt_after": (
                "Intentar resolver la consulta con las herramientas disponibles. "
                "Solo derivar a soporte si el problema requiere acceso a sistemas "
                "internos no disponibles para el agente."
            ),
            "impact_estimate": "alto",
        }
    ]
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def loop_seeded(postgres_engine: AsyncEngine) -> AsyncIterator[dict]:
    """Seed 1 org, 1 project, 4 agents, 12 evaluations (3 per agent, score<50)."""
    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)

    org_id = uuid.uuid4()
    proj_id = uuid.uuid4()
    proj_public_id = f"test_loop_{proj_id.hex[:12]}"
    sub_agents = ["productos", "pedidos", "devoluciones", "pagos"]
    agent_ids = {slug: uuid.uuid4() for slug in sub_agents}
    conv_ids: list[uuid.UUID] = []
    eval_ids: list[uuid.UUID] = []

    from cryptography.fernet import Fernet

    ws = Fernet(Fernet.generate_key()).encrypt(b"test-secret")

    async with factory() as s:
        async with s.begin():
            await s.execute(
                text(
                    "INSERT INTO organizations(id, public_id, slug, name) "
                    "VALUES (:id, :pid, :slug, :name)"
                ),
                {
                    "id": org_id,
                    "pid": f"org_{org_id.hex[:16]}",
                    "slug": f"org-loop-{org_id.hex[:6]}",
                    "name": "Loop Test Org",
                },
            )
            await s.execute(
                text(
                    "INSERT INTO projects"
                    "(id, public_id, org_id, slug, name, webhook_secret_encrypted) "
                    "VALUES (:id, :pid, :oid, :slug, :name, :ws)"
                ),
                {
                    "id": proj_id,
                    "pid": proj_public_id,
                    "oid": org_id,
                    "slug": f"proj-loop-{proj_id.hex[:6]}",
                    "name": "Loop Test Project",
                    "ws": ws,
                },
            )
            for slug, aid in agent_ids.items():
                await s.execute(
                    text(
                        "INSERT INTO agents"
                        "(id, public_id, project_id, org_id, slug, name, platform) "
                        "VALUES (:id, :pid, :proj, :org, :slug, :name, :plat)"
                    ),
                    {
                        "id": aid,
                        "pid": f"agt_{aid.hex[:16]}",
                        "proj": proj_id,
                        "org": org_id,
                        "slug": slug,
                        "name": slug.capitalize(),
                        "plat": "custom_sdk",
                    },
                )
            # 3 conversations + 3 evaluations per sub-agent
            for slug, aid in agent_ids.items():
                for i in range(3):
                    cid = uuid.uuid4()
                    eid = uuid.uuid4()
                    conv_ids.append(cid)
                    eval_ids.append(eid)
                    await s.execute(
                        text(
                            "INSERT INTO conversations"
                            "(id, public_id, project_id, org_id, agent_id,"
                            " external_id, platform) "
                            "VALUES (:id, :pid, :proj, :org, :agent, :ext, :plat)"
                        ),
                        {
                            "id": cid,
                            "pid": f"conv_{cid.hex[:16]}",
                            "proj": proj_id,
                            "org": org_id,
                            "agent": aid,
                            "ext": f"ext-{cid.hex[:8]}",
                            "plat": "custom_sdk",
                        },
                    )
                    await s.execute(
                        text(
                            "INSERT INTO evaluations"
                            "(id, public_id, conversation_id, project_id, org_id,"
                            " agent_id, score, resolution, frustration, topic, summary) "
                            "VALUES (:id, :pid, :conv, :proj, :org, :agent,"
                            " :score, :res, :frus, :topic, :summary)"
                        ),
                        {
                            "id": eid,
                            "pid": f"eval_{eid.hex[:16]}",
                            "conv": cid,
                            "proj": proj_id,
                            "org": org_id,
                            "agent": aid,
                            "score": 35 + i * 5,  # 35, 40, 45 — all < 50
                            "res": False,
                            "frus": True,
                            "topic": f"consulta_{slug}",
                            "summary": (
                                f"El agente {slug} no pudo resolver la consulta "
                                f"y derivó al soporte sin intentar una solución. "
                                f"El usuario quedó frustrado (conv #{i+1})."
                            ),
                        },
                    )

    yield {
        "proj_public_id": proj_public_id,
        "proj_id": proj_id,
        "org_id": org_id,
        "agent_ids": agent_ids,
        "conv_ids": conv_ids,
        "eval_ids": eval_ids,
    }

    # Cleanup — cascade handles children
    async with factory() as s:
        async with s.begin():
            await s.execute(
                text("DELETE FROM organizations WHERE id = :id"), {"id": org_id}
            )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOptimizationLoopService:
    @pytest.mark.asyncio
    async def test_run_loop_4_patterns_persisted(
        self,
        postgres_engine: AsyncEngine,
        loop_seeded: dict,
    ) -> None:
        """Happy path: 4 sub-agents × canned Sonnet response → 4 improvements in DB."""
        factory = async_sessionmaker(postgres_engine, expire_on_commit=False)

        input_ = OptimizationLoopInput(
            project_public_id=loop_seeded["proj_public_id"],
            limit=50,
            sub_agents=["productos", "pedidos", "devoluciones", "pagos"],
            prompt_version_label="v1",
            dry_run=False,
        )

        # Build a mock Anthropic client whose messages.create returns a
        # response shaped like the real anthropic SDK object.
        def _make_fake_response():
            block = MagicMock()
            block.type = "text"
            block.text = json.dumps(_CANNED_PATTERN)
            usage = MagicMock()
            usage.input_tokens = 800
            usage.output_tokens = 300
            resp = MagicMock()
            resp.content = [block]
            resp.usage = usage
            return resp

        mock_create = AsyncMock(side_effect=lambda **_kw: _make_fake_response())
        mock_messages = MagicMock()
        mock_messages.create = mock_create
        mock_client = MagicMock()
        mock_client.messages = mock_messages

        with (
            patch(
                "app.services.optimization_loop.AsyncAnthropic",
                return_value=mock_client,
            ),
            patch(
                "app.services.optimization_loop.get_settings",
                return_value=MagicMock(anthropic_api_key="sk-test-fake"),
            ),
            patch(
                "app.services.optimization_loop._load_prompt_file",
                return_value="prompt v1 placeholder",
            ),
        ):
            async with factory() as session:
                output = await run_optimization_loop(input_, session)

        # --- assertions ---
        assert (
            output.sub_agents_analyzed == 4
        ), f"expected 4 sub-agents analyzed, got {output.sub_agents_analyzed}"
        assert (
            len(output.patterns) == 4
        ), f"expected 4 patterns, got {len(output.patterns)}"
        assert output.sonnet_cost_usd > 0, "cost must be > 0 when Sonnet was called"
        assert len(output.improvements_persisted_ids) == 4

        # Verify all 4 rows exist in DB with status=pending
        async with factory() as session:
            for imp_id in output.improvements_persisted_ids:
                result = await session.execute(
                    text(
                        "SELECT status, agent_slug FROM improvements WHERE public_id = :pid"
                    ),
                    {"pid": imp_id},
                )
                row = result.one_or_none()
                assert row is not None, f"improvement {imp_id} not found in DB"
                assert (
                    row.status == "pending"
                ), f"expected status=pending, got {row.status}"

    @pytest.mark.asyncio
    async def test_dry_run_no_db_writes(
        self,
        postgres_engine: AsyncEngine,
        loop_seeded: dict,
    ) -> None:
        """dry_run=True: no Sonnet calls, no DB writes, sub_agents_analyzed still counted."""
        factory = async_sessionmaker(postgres_engine, expire_on_commit=False)

        input_ = OptimizationLoopInput(
            project_public_id=loop_seeded["proj_public_id"],
            limit=50,
            sub_agents=["productos", "pedidos"],
            dry_run=True,
        )

        mock_client = MagicMock()

        with (
            patch(
                "app.services.optimization_loop.AsyncAnthropic",
                return_value=mock_client,
            ),
            patch(
                "app.services.optimization_loop.get_settings",
                return_value=MagicMock(anthropic_api_key="sk-test-fake"),
            ),
        ):
            async with factory() as session:
                output = await run_optimization_loop(input_, session)

        assert output.sub_agents_analyzed == 2
        assert output.patterns == []
        assert output.improvements_persisted_ids == []
        assert output.sonnet_cost_usd == 0.0
        # Sonnet should never have been called
        mock_client.messages.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_project_not_found_raises(
        self,
        postgres_engine: AsyncEngine,
    ) -> None:
        """If project_public_id doesn't exist, raises ValueError."""
        factory = async_sessionmaker(postgres_engine, expire_on_commit=False)

        input_ = OptimizationLoopInput(
            project_public_id="nonexistent_proj_xyz",
            limit=10,
            sub_agents=["productos"],
        )

        with (
            patch(
                "app.services.optimization_loop.get_settings",
                return_value=MagicMock(anthropic_api_key="sk-test-fake"),
            ),
        ):
            async with factory() as session:
                with pytest.raises(ValueError, match="project not found"):
                    await run_optimization_loop(input_, session)
