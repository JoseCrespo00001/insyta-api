"""process_upload Celery task.

Driven by `POST /api/v1/uploads/csv`. Steps:
  1. Load the upload row and its CSV bytes from storage.
  2. Parse the CSV (custom_sdk canonical columns: conversation_id, role, content, timestamp).
  3. For each parsed conversation: upsert idempotently, insert its messages (with
     stable `seq`), set preview/message_count/status.
  4. Update the upload status counters.

Evaluation is NOT enqueued here — the LLM-as-judge runs at audit time
(`app.workers.audit.run_audit`) over the conversations the user selects.

The CSV reading is sync; DB writes are async. Celery tasks are sync so we wrap
the async logic with `asyncio.run`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.core.db import engine, tenant_txn
from app.models import Message, Upload
from app.services.anonymizer import anonymize
from app.services.celery_app import celery_app
from app.services.idempotency import upsert_conversation_idempotent
from app.workers.parsers import ConversationDTO, get_parser

logger = logging.getLogger(__name__)

DEFAULT_PLATFORM = "custom_sdk"


async def _load_upload_blob(
    upload_id: uuid.UUID, org_id: uuid.UUID
) -> tuple[bytes, str, dict]:
    """Load the CSV bytes + the metadata the processor needs.

    Returns (csv_bytes, platform, meta) where meta has project_id, org_id,
    agent_id, upload_id. Runs under the tenant GUC so the RLS-protected
    `uploads` row is visible even on Supabase (FORCE RLS, non-superuser role).
    """
    async with tenant_txn(org_id) as session:
        # undefer(raw_content): la columna es deferred (no se trae en los selects
        # de status), pero acá SÍ necesitamos los bytes del CSV.
        upload = (
            await session.execute(
                select(Upload)
                .where(Upload.id == upload_id)
                .options(undefer(Upload.raw_content))
            )
        ).scalar_one_or_none()
        if upload is None:
            raise ValueError(f"upload {upload_id} not found")
        if upload.agent_id is None:
            raise ValueError(f"upload {upload_id} has no agent_id")
        if upload.raw_content is None:
            raise ValueError(
                f"upload {upload_id} has no raw_content (CSV vacío o ya limpiado)"
            )
        csv_bytes = bytes(upload.raw_content)
        meta = {
            "project_id": str(upload.project_id),
            "org_id": str(upload.org_id),
            "agent_id": str(upload.agent_id),
            "upload_id": str(upload.id),
            "filename": upload.filename or "",
        }
    # WhatsApp export (.txt) vs CSV canónico (custom_sdk).
    platform = (
        "whatsapp" if meta["filename"].lower().endswith(".txt") else DEFAULT_PLATFORM
    )
    return csv_bytes, platform, meta


_PHONE_RE = re.compile(r"^\+?\d[\d\s().-]{6,18}\d$")


def _phone_from_external(external_id: str | None) -> str | None:
    """Devuelve un teléfono normalizado si el external_id tiene pinta de número
    (caso típico WhatsApp), si no None. Solo dígitos + '+' inicial opcional."""
    if not external_id:
        return None
    raw = external_id.strip()
    if not _PHONE_RE.match(raw):
        return None
    digits = re.sub(r"\D", "", raw)
    if not 8 <= len(digits) <= 15:
        return None
    return f"+{digits}" if raw.startswith("+") else digits


def _preview(messages: Iterable) -> str:
    for dto in messages:
        if dto.role == "user" and dto.content:
            return dto.content[:160]
    for dto in messages:
        if dto.content:
            return dto.content[:160]
    return ""


async def _persist_messages(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    project_id: uuid.UUID,
    org_id: uuid.UUID,
    dtos: list,
) -> int:
    rows = []
    for seq, dto in enumerate(dtos):
        rows.append(
            {
                "id": uuid.uuid4(),
                "public_id": f"msg_{uuid.uuid4().hex[:16]}",
                "conversation_id": conversation_id,
                "project_id": project_id,
                "org_id": org_id,
                "seq": seq,
                "role": dto.role,
                "content": dto.content,
                # PII tokenizada: nunca debe llegar cruda al LLM (iron rule).
                # Los judges leen content_anonymized con fallback a content.
                "content_anonymized": (
                    anonymize(dto.content).text if dto.content else None
                ),
                "timestamp": dto.timestamp,
            }
        )
    if not rows:
        return 0
    await session.execute(Message.__table__.insert().values(rows))
    return len(rows)


async def process_conversations(
    *,
    parsed: Iterable[ConversationDTO],
    project_id: uuid.UUID,
    org_id: uuid.UUID,
    agent_id: uuid.UUID,
    upload_id: uuid.UUID | None = None,
) -> dict:
    """Bulk-process parsed DTOs into conversations + messages. Returns counters."""
    new_count = 0
    dup_count = 0
    msg_count = 0
    # Cada N conversaciones escribimos rows_processed para que la barra del front
    # (polling cada 1200ms) suba gradualmente en vez de saltar de 0 al total.
    progress_step = 5
    for i, dto in enumerate(parsed):
        msgs = list(dto.messages)
        async with tenant_txn(org_id) as session:
            conv, was_new = await upsert_conversation_idempotent(
                session,
                agent_id=agent_id,
                external_id=dto.external_id,
                project_id=project_id,
                org_id=org_id,
                platform=dto.platform,
                public_id=f"conv_{uuid.uuid4().hex[:16]}",
                started_at=dto.started_at,
                upload_id=upload_id,
                extra={
                    "preview": _preview(msgs),
                    "message_count": len(msgs),
                    "status": "completed",
                    "contact_name": dto.contact_name,
                    # Identificador de usuario para CSV/campaña y reputación.
                    # Si el parser no lo trajo, derivamos del external_id cuando
                    # tiene pinta de teléfono (WhatsApp: el external_id ES el número).
                    "contact_phone": dto.contact_phone
                    or _phone_from_external(dto.external_id),
                    "ended_at": msgs[-1].timestamp if msgs else None,
                },
            )
            if was_new:
                msg_count += await _persist_messages(
                    session,
                    conversation_id=conv.id,
                    project_id=project_id,
                    org_id=org_id,
                    dtos=msgs,
                )
                new_count += 1
            else:
                dup_count += 1
        # Progreso parcial (fuera de la txn de la conversación, throttled).
        if upload_id is not None and (i + 1) % progress_step == 0:
            await _set_upload_status(
                upload_id, org_id, rows_processed=new_count + dup_count
            )
    return {
        "new_conversations": new_count,
        "duplicate_conversations": dup_count,
        "messages_persisted": msg_count,
    }


async def _set_upload_status(upload_id: uuid.UUID, org_id: uuid.UUID, **fields) -> None:
    async with tenant_txn(org_id) as session:
        await session.execute(
            update(Upload).where(Upload.id == upload_id).values(**fields)
        )


async def _run(upload_id: uuid.UUID, org_id: uuid.UUID) -> dict:
    csv_bytes, platform, meta = await _load_upload_blob(upload_id, org_id)
    parser = get_parser(platform)
    parsed = list(parser(csv_bytes))
    project_id = uuid.UUID(meta["project_id"])
    org_id = uuid.UUID(meta["org_id"])
    agent_id = uuid.UUID(meta["agent_id"])

    # Si el CSV no trajo conversaciones (columnas erróneas), fallar con mensaje
    # claro en vez de "completar" con 0 en silencio.
    if not parsed:
        await _set_upload_status(
            upload_id,
            org_id,
            status="failed",
            rows_total=0,
            rows_processed=0,
            error_message=(
                "No se detectaron conversaciones. El CSV debe tener columnas: "
                "conversation_id, role, content, timestamp."
            ),
            finished_at=datetime.now(timezone.utc),
        )
        return {"upload_id": str(upload_id), "parsed_conversations": 0}

    await _set_upload_status(
        upload_id,
        org_id,
        status="processing",
        rows_total=len(parsed),
        rows_processed=0,
        started_at=datetime.now(timezone.utc),
    )

    summary = await process_conversations(
        parsed=parsed,
        project_id=project_id,
        org_id=org_id,
        agent_id=agent_id,
        upload_id=upload_id,
    )

    await _set_upload_status(
        upload_id,
        org_id,
        status="completed",
        rows_processed=summary["new_conversations"]
        + summary["duplicate_conversations"],
        finished_at=datetime.now(timezone.utc),
        # Limpia el CSV crudo: ya se procesó, no hace falta guardar el blob.
        raw_content=None,
    )

    summary["upload_id"] = str(upload_id)
    summary["parsed_conversations"] = len(parsed)
    return summary


async def _run_and_dispose(upload_id: uuid.UUID, org_id: uuid.UUID) -> dict:
    # Each Celery task runs in a fresh asyncio loop; dispose the shared engine
    # at the end so pooled asyncpg connections don't leak across loops.
    try:
        return await _run(upload_id, org_id)
    except Exception as exc:
        # Sin este handler, cualquier excepción antes de marcar "processing"
        # (p.ej. el CSV no está en Storage) dejaba el upload pegado en "pending"
        # para siempre. Marcamos "failed" con el error y re-lanzamos para que
        # Celery lo registre como FAILURE.
        logger.exception("[UPLOADS] process_upload falló upload=%s", upload_id)
        try:
            await _set_upload_status(
                upload_id,
                org_id,
                status="failed",
                error_message=str(exc)[:1000],
                finished_at=datetime.now(timezone.utc),
            )
        except Exception:
            # No ocultar el error original si el UPDATE de status también falla.
            logger.exception("[UPLOADS] no se pudo marcar failed upload=%s", upload_id)
        raise
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.processor.process_upload", bind=True)
def process_upload(self, upload_id: str, org_id: str) -> dict:
    return asyncio.run(_run_and_dispose(uuid.UUID(upload_id), uuid.UUID(org_id)))
