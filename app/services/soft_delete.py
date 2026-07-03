"""Soft-delete de subtrees.

Regla del proyecto: los endpoints DELETE nunca borran filas; setean
`is_deleted=True` (+ `deleted_at`) en la raíz y sus descendientes. Como el
soft-delete no dispara los `ON DELETE CASCADE` de las FK, acá replicamos la
cascada en código.

Todas las tablas soft-deletables cuelgan de `project_id`, y las de nivel
conversación tienen `conversation_id`, así que la cascada es un UPDATE masivo
por tabla. Corre dentro de la sesión del request (GUCs de tenant seteados), así
que RLS garantiza el aislamiento multi-tenant.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import ColumnElement, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, Conversation, Flow, Message, Project, Supervisor, Upload
from app.models.audits import Audit, AuditConversation, MessageEvaluation
from app.models.flow_versions import FlowVersion
from app.models.improvements import Improvement, ImprovementConversation
from app.models.tenancy import Evaluation

# Tablas que cuelgan directo de un proyecto (todas tienen project_id).
_PROJECT_CHILDREN = (
    Agent,
    Conversation,
    Message,
    Evaluation,
    Upload,
    Flow,
    FlowVersion,
    Audit,
    AuditConversation,
    MessageEvaluation,
    Improvement,
    ImprovementConversation,
    Supervisor,
)

# Tablas que cuelgan de una conversación (todas tienen conversation_id).
_CONVERSATION_CHILDREN = (
    Message,
    Evaluation,
    AuditConversation,
    MessageEvaluation,
    ImprovementConversation,
)


async def _flag(session: AsyncSession, model: Any, where: ColumnElement[bool]) -> None:
    """Marca is_deleted=True + deleted_at=now en las filas de `model` que
    matchean `where` y todavía no están borradas (para no pisar deleted_at)."""
    await session.execute(
        update(model)
        .where(where, model.is_deleted.is_(False))
        .values(is_deleted=True, deleted_at=datetime.now(timezone.utc))
    )


async def soft_delete_project(session: AsyncSession, project_id: uuid.UUID) -> None:
    for model in _PROJECT_CHILDREN:
        await _flag(session, model, model.project_id == project_id)
    await _flag(session, Project, Project.id == project_id)


async def soft_delete_conversations(
    session: AsyncSession, conversation_ids: Sequence[uuid.UUID]
) -> None:
    if not conversation_ids:
        return
    for model in _CONVERSATION_CHILDREN:
        await _flag(session, model, model.conversation_id.in_(conversation_ids))
    await _flag(session, Conversation, Conversation.id.in_(conversation_ids))


async def soft_delete_upload(session: AsyncSession, upload_id: uuid.UUID) -> None:
    conv_ids = (
        (
            await session.execute(
                select(Conversation.id)
                .where(Conversation.upload_id == upload_id)
                .execution_options(include_deleted=True)
            )
        )
        .scalars()
        .all()
    )
    await soft_delete_conversations(session, conv_ids)
    await _flag(session, Upload, Upload.id == upload_id)


async def soft_delete_supervisor(
    session: AsyncSession, supervisor_id: uuid.UUID
) -> None:
    # Leaf: las auditorías lo referencian con SET NULL, no hay subtree.
    await _flag(session, Supervisor, Supervisor.id == supervisor_id)


async def soft_delete_flow(session: AsyncSession, flow_id: uuid.UUID) -> None:
    impr_ids = (
        (
            await session.execute(
                select(Improvement.id)
                .where(Improvement.flow_id == flow_id)
                .execution_options(include_deleted=True)
            )
        )
        .scalars()
        .all()
    )
    if impr_ids:
        await _flag(
            session,
            ImprovementConversation,
            ImprovementConversation.improvement_id.in_(impr_ids),
        )
        await _flag(session, Improvement, Improvement.id.in_(impr_ids))
    await _flag(session, FlowVersion, FlowVersion.flow_id == flow_id)
    await _flag(session, Flow, Flow.id == flow_id)
