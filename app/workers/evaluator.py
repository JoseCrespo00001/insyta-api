"""evaluate_conversation Celery task (EQUIP-64).

Loads a conversation + its messages, asks the LLM router for an evaluation
(retry x3 on transient errors), and persists the result. Idempotent: the
`evaluations.conversation_id UNIQUE` constraint guarantees a second call for
the same conversation is a no-op (we ON CONFLICT DO NOTHING).
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session_factory
from app.llm.router import FatalLLMError, LLMRouter, TransientLLMError
from app.models import Conversation, Evaluation, Message
from app.services.celery_app import celery_app

logger = logging.getLogger(__name__)


async def _load_conversation(
    session: AsyncSession, conv_id: uuid.UUID
) -> tuple[Conversation, list[dict]]:
    conv = await session.get(Conversation, conv_id)
    if conv is None:
        raise ValueError(f"conversation {conv_id} not found")
    msgs_result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conv_id)
        .order_by(Message.timestamp.asc())
    )
    msgs = [
        {
            "role": m.role,
            "content": m.content,
            "content_anonymized": m.content_anonymized,
        }
        for m in msgs_result.scalars()
    ]
    return conv, msgs


async def _persist_evaluation(
    session: AsyncSession,
    *,
    conv: Conversation,
    parsed,
    usage,
) -> bool:
    """Insert evaluation if not already present. Returns True on insert."""
    public_id = f"eval_{uuid.uuid4().hex[:16]}"
    stmt = (
        pg_insert(Evaluation)
        .values(
            id=uuid.uuid4(),
            public_id=public_id,
            conversation_id=conv.id,
            project_id=conv.project_id,
            org_id=conv.org_id,
            agent_id=conv.agent_id,
            score=parsed.score,
            resolution=parsed.resolution,
            satisfaction=parsed.satisfaction,
            tone=parsed.tone,
            frustration=parsed.frustration,
            escalated=parsed.escalated,
            efficiency=parsed.efficiency,
            scope_violation=parsed.scope_violation,
            topic=parsed.topic,
            summary=parsed.summary,
            model_used=usage.model,
            tokens_used=usage.input_tokens + usage.output_tokens,
            cost_usd=usage.cost_usd,
        )
        .on_conflict_do_nothing(index_elements=["conversation_id"])
        .returning(Evaluation.id)
    )
    result = await session.execute(stmt)
    inserted = result.scalar_one_or_none()
    await session.commit()
    return inserted is not None


async def _run_eval(conv_id: uuid.UUID) -> dict:
    router = LLMRouter()
    last_exc: Exception | None = None
    parsed = None
    usage = None
    for attempt in range(3):
        try:
            async with async_session_factory() as session:
                _, msgs = await _load_conversation(session, conv_id)
            parsed, usage = await router.evaluate(msgs)
            break
        except TransientLLMError as exc:
            last_exc = exc
            logger.warning(
                "[EVAL] transient error on conv=%s attempt=%d: %s",
                conv_id,
                attempt + 1,
                exc,
            )
            await asyncio.sleep(0.5 * (2**attempt))
        except FatalLLMError as exc:
            logger.error("[EVAL] fatal LLM error on conv=%s: %s", conv_id, exc)
            raise
    if parsed is None or usage is None:
        raise RuntimeError(
            f"evaluation failed after 3 attempts: {last_exc}"
        ) from last_exc

    async with async_session_factory() as session:
        conv, _ = await _load_conversation(session, conv_id)
        was_new = await _persist_evaluation(
            session, conv=conv, parsed=parsed, usage=usage
        )

    logger.info(
        "[EVAL] conv=%s model=%s tokens=%d cost=%s cache_read=%d new=%s",
        conv_id,
        usage.model,
        usage.input_tokens + usage.output_tokens,
        usage.cost_usd,
        usage.cache_read_input_tokens,
        was_new,
    )
    return {
        "conversation_id": str(conv_id),
        "model": usage.model,
        "score": parsed.score,
        "cost_usd": str(usage.cost_usd),
        "cache_read_input_tokens": usage.cache_read_input_tokens,
        "input_tokens": usage.input_tokens,
        "was_new": was_new,
    }


@celery_app.task(name="app.workers.evaluator.evaluate_conversation", bind=True)
def evaluate_conversation(self, conversation_id: str) -> dict:
    conv_id = uuid.UUID(conversation_id)
    return asyncio.run(_run_eval(conv_id))
