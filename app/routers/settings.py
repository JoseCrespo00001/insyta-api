"""Settings del tenant — API keys de proveedores LLM (Anthropic, DeepSeek).

Cada key se guarda cifrada (Fernet) en `organizations.{provider}_api_key_encrypted`.
Nunca se devuelve en claro: GET responde si está configurada + un masked.
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

# provider -> columna cifrada en organizations
_PROVIDER_COL = {
    "anthropic": "anthropic_api_key_encrypted",
    "deepseek": "deepseek_api_key_encrypted",
}


class _Camel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class LlmKeyStatus(_Camel):
    provider: str
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


def _check_provider(provider: str) -> str:
    col = _PROVIDER_COL.get(provider)
    if col is None:
        raise HTTPException(status_code=404, detail="Proveedor no soportado")
    return col


def _status(provider: str, enc: str | None) -> LlmKeyStatus:
    if not enc:
        return LlmKeyStatus(provider=provider, configured=False)
    plain = decrypt_secret(enc)
    return LlmKeyStatus(
        provider=provider,
        configured=bool(plain),
        masked=mask_secret(plain) if plain else None,
    )


@router.get("/settings/llm-keys", response_model=list[LlmKeyStatus])
async def list_llm_keys(
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> list[LlmKeyStatus]:
    org = await _get_org(session, current_user.org_id)
    return [_status(prov, getattr(org, col)) for prov, col in _PROVIDER_COL.items()]


@router.put("/settings/llm-keys/{provider}", response_model=LlmKeyStatus)
async def set_llm_key(
    provider: str,
    payload: LlmKeyUpdate,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> LlmKeyStatus:
    col = _check_provider(provider)
    key = payload.api_key.strip()
    if len(key) < 8:
        raise HTTPException(status_code=422, detail="API key inválida")
    org = await _get_org(session, current_user.org_id)
    setattr(org, col, encrypt_secret(key))
    await session.commit()
    logger.info("[SETTINGS] %s key set for org=%s", provider, current_user.org_id)
    return LlmKeyStatus(provider=provider, configured=True, masked=mask_secret(key))


@router.delete("/settings/llm-keys/{provider}", response_model=LlmKeyStatus)
async def delete_llm_key(
    provider: str,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_with_tenant_context),
) -> LlmKeyStatus:
    col = _check_provider(provider)
    org = await _get_org(session, current_user.org_id)
    setattr(org, col, None)
    await session.commit()
    return LlmKeyStatus(provider=provider, configured=False)
