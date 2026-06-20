"""GET /api/v1/me — the authenticated user's identity + tenant + org name."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy import select

from app.core.auth import CurrentUser, get_current_user
from app.core.db import async_session_factory
from app.models import Organization, User

router = APIRouter(prefix="/api/v1", tags=["me"])


class MeResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    user_id: str
    email: str
    full_name: str | None
    org_id: uuid.UUID
    org_name: str | None
    role: str
    allowed_project_ids: list[uuid.UUID]


@router.get("/me", response_model=MeResponse)
async def me(current_user: CurrentUser = Depends(get_current_user)) -> MeResponse:
    full_name: str | None = None
    role = "owner"
    org_name: str | None = None
    # Enrich from the DB (users/organizations are queried pre-tenant). Best-effort:
    # if the DB is unavailable we still return the claim-based identity.
    try:
        async with async_session_factory() as session:
            row = (
                await session.execute(
                    select(User.full_name, User.role, Organization.name)
                    .join(Organization, Organization.id == User.org_id)
                    .where(User.supabase_user_id == current_user.user_id)
                )
            ).one_or_none()
            if row is not None:
                full_name, role, org_name = row.full_name, row.role, row.name
    except Exception:
        pass

    return MeResponse(
        user_id=current_user.user_id,
        email=current_user.email,
        full_name=full_name,
        org_id=current_user.org_id,
        org_name=org_name,
        role=role,
        allowed_project_ids=current_user.allowed_project_ids,
    )
