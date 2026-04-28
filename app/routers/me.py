"""GET /api/v1/me — returns the authenticated user's identity + tenant scope."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import CurrentUser, get_current_user

router = APIRouter(prefix="/api/v1", tags=["me"])


class MeResponse(BaseModel):
    user_id: str
    email: str
    org_id: uuid.UUID
    allowed_project_ids: list[uuid.UUID]


@router.get("/me", response_model=MeResponse)
async def me(current_user: CurrentUser = Depends(get_current_user)) -> MeResponse:
    return MeResponse(
        user_id=current_user.user_id,
        email=current_user.email,
        org_id=current_user.org_id,
        allowed_project_ids=current_user.allowed_project_ids,
    )
