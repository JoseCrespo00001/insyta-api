"""Idempotency requires PostgreSQL (ON CONFLICT). Skips if DB unavailable."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.services.idempotency import upsert_conversation_idempotent


@pytest.mark.asyncio
async def test_first_call_inserts_and_marks_new(
    postgres_engine: AsyncEngine, seeded_two_orgs: dict
):
    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with factory() as s:
        async with s.begin():
            conv, was_new = await upsert_conversation_idempotent(
                s,
                agent_id=seeded_two_orgs["agent_a"],
                external_id="ext-new-1",
                project_id=seeded_two_orgs["proj_a"],
                org_id=seeded_two_orgs["org_a"],
                platform="custom_sdk",
                public_id=f"conv_{uuid.uuid4().hex[:16]}",
            )
            assert was_new is True
            assert conv.external_id == "ext-new-1"
        async with s.begin():
            await s.execute(
                text("DELETE FROM conversations WHERE external_id = 'ext-new-1'")
            )


@pytest.mark.asyncio
async def test_second_call_returns_existing_row_and_marks_not_new(
    postgres_engine: AsyncEngine, seeded_two_orgs: dict
):
    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with factory() as s:
        async with s.begin():
            first, first_new = await upsert_conversation_idempotent(
                s,
                agent_id=seeded_two_orgs["agent_a"],
                external_id="ext-dup-1",
                project_id=seeded_two_orgs["proj_a"],
                org_id=seeded_two_orgs["org_a"],
                platform="custom_sdk",
                public_id=f"conv_{uuid.uuid4().hex[:16]}",
            )
        async with s.begin():
            second, second_new = await upsert_conversation_idempotent(
                s,
                agent_id=seeded_two_orgs["agent_a"],
                external_id="ext-dup-1",
                project_id=seeded_two_orgs["proj_a"],
                org_id=seeded_two_orgs["org_a"],
                platform="custom_sdk",
                public_id=f"conv_{uuid.uuid4().hex[:16]}",
            )

        assert first_new is True
        assert second_new is False
        assert first.id == second.id

        async with s.begin():
            await s.execute(
                text("DELETE FROM conversations WHERE external_id = 'ext-dup-1'")
            )


@pytest.mark.asyncio
async def test_ten_identical_calls_yield_one_row(
    postgres_engine: AsyncEngine, seeded_two_orgs: dict
):
    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with factory() as s:
        new_count = 0
        ids = set()
        for _ in range(10):
            async with s.begin():
                conv, was_new = await upsert_conversation_idempotent(
                    s,
                    agent_id=seeded_two_orgs["agent_a"],
                    external_id="ext-many-1",
                    project_id=seeded_two_orgs["proj_a"],
                    org_id=seeded_two_orgs["org_a"],
                    platform="custom_sdk",
                    public_id=f"conv_{uuid.uuid4().hex[:16]}",
                )
                ids.add(conv.id)
                if was_new:
                    new_count += 1

        async with s.begin():
            count = await s.execute(
                text(
                    "SELECT count(*) FROM conversations "
                    "WHERE agent_id = :a AND external_id = 'ext-many-1'"
                ),
                {"a": seeded_two_orgs["agent_a"]},
            )
            assert count.scalar_one() == 1
            await s.execute(
                text("DELETE FROM conversations WHERE external_id = 'ext-many-1'")
            )

        assert new_count == 1
        assert len(ids) == 1


@pytest.mark.asyncio
async def test_same_external_id_different_agents_inserts_two(
    postgres_engine: AsyncEngine, seeded_two_orgs: dict
):
    """Uniqueness is (agent_id, external_id), so two agents may share external_id."""
    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with factory() as s:
        async with s.begin():
            _, new_a = await upsert_conversation_idempotent(
                s,
                agent_id=seeded_two_orgs["agent_a"],
                external_id="ext-shared",
                project_id=seeded_two_orgs["proj_a"],
                org_id=seeded_two_orgs["org_a"],
                platform="custom_sdk",
                public_id=f"conv_{uuid.uuid4().hex[:16]}",
            )
            _, new_b = await upsert_conversation_idempotent(
                s,
                agent_id=seeded_two_orgs["agent_b"],
                external_id="ext-shared",
                project_id=seeded_two_orgs["proj_b"],
                org_id=seeded_two_orgs["org_b"],
                platform="custom_sdk",
                public_id=f"conv_{uuid.uuid4().hex[:16]}",
            )

        assert new_a is True
        assert new_b is True

        async with s.begin():
            await s.execute(
                text("DELETE FROM conversations WHERE external_id = 'ext-shared'")
            )


def test_function_is_async():
    """Sanity check: the idempotency function is awaitable."""
    assert asyncio.iscoroutinefunction(upsert_conversation_idempotent)
