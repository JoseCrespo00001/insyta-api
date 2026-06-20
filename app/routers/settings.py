"""Settings del tenant — API key del proveedor LLM (Anthropic).

La key se guarda cifrada (Fernet) en `organizations.anthropic_api_key_encrypted`.
Nunca se devuelve en claro: GET responde solo si está configurada + un masked.
El worker `run_audit` la desencripta y la usa para correr el judge.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.db import get_db_with_tenant_context
from app.models import Organization
from app.services.secret_crypto import decrypt_secret, encrypt_secret, mask_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["settings"])


class _Camel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class LlmKeyStatus(_Camel):
    provider: str = "anthropic"
    configured: bool
    masked: str | None = None


class LlmKeyUpdate(_Camel):
    api_key: str


async def _get_org(session: AsyncSession, org_id) -> Organization:
    org = (
        await session.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org


@router.get("/settings/llm-key", response_model=LlmKeyStatus)
async def get_llm_key(
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> LlmKeyStatus:
    org = await _get_org(session, current_user.org_id)
    enc = org.anthropic_api_key_encrypted
    if not enc:
        return LlmKeyStatus(configured=False)
    plain = decrypt_secret(enc)
    return LlmKeyStatus(
        configured=bool(plain),
        masked=mask_secret(plain) if plain else None,
    )


@router.put("/settings/llm-key", response_model=LlmKeyStatus)
async def set_llm_key(
    payload: LlmKeyUpdate,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> LlmKeyStatus:
    key = payload.api_key.strip()
    if len(key) < 8:
        raise HTTPException(status_code=422, detail="API key inválida")
    org = await _get_org(session, current_user.org_id)
    org.anthropic_api_key_encrypted = encrypt_secret(key)
    await session.commit()
    logger.info("[SETTINGS] LLM key set for org=%s", current_user.org_id)
    return LlmKeyStatus(configured=True, masked=mask_secret(key))


@router.delete("/settings/llm-key", response_model=LlmKeyStatus)
async def delete_llm_key(
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> LlmKeyStatus:
    org = await _get_org(session, current_user.org_id)
    org.anthropic_api_key_encrypted = None
    await session.commit()
    return LlmKeyStatus(configured=False)
