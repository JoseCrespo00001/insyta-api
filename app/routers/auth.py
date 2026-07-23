"""Auth bootstrap — provision an Organization + User on first login.

Supabase issues the JWT (sub + email); it has no org. The frontend calls
`POST /api/v1/auth/bootstrap` once after sign-in: if the user already exists we
return their org, otherwise we create a customer Organization and an owner User.
After this, `get_current_user` resolves the org from the users table.

Runs on a plain session (no tenant context): it is creating the tenant itself.
"""

from __future__ import annotations

import logging
import re
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select, text

from app.core.auth import TokenIdentity, get_token_identity
from app.core.db import async_session_factory
from app.core.ratelimit import limiter
from app.models import Organization, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class BootstrapResponse(BaseModel):
    user_id: str
    org_id: uuid.UUID
    email: str
    created: bool


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or f"org-{uuid.uuid4().hex[:8]}"


@router.post("/bootstrap", response_model=BootstrapResponse)
@limiter.limit("10/minute")  # signup abierto: frena bootstraps masivos por IP
async def bootstrap(
    request: Request,
    identity: TokenIdentity = Depends(get_token_identity),
) -> BootstrapResponse:
    async with async_session_factory() as session:
        # One transaction: read (existence) + write (create). Setting the tenant
        # GUC before the org INSERT makes the organizations RLS WITH CHECK
        # (id = current_org) pass even under FORCE RLS (Supabase, non-superuser).
        async with session.begin():
            existing = (
                await session.execute(
                    select(User).where(User.supabase_user_id == identity.user_id)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return BootstrapResponse(
                    user_id=existing.public_id,
                    org_id=existing.org_id,
                    email=existing.email,
                    created=False,
                )

            org_id = uuid.uuid4()
            local_part = identity.email.split("@")[0]
            org = Organization(
                id=org_id,
                public_id=f"org_{org_id.hex[:24]}",
                slug=f"{_slugify(local_part)}-{org_id.hex[:6]}",
                name=f"Organización de {local_part}",
                org_type="customer",
            )
            user_id = uuid.uuid4()
            user = User(
                id=user_id,
                public_id=f"usr_{user_id.hex[:24]}",
                supabase_user_id=identity.user_id,
                email=identity.email,
                full_name=None,
                org_id=org_id,
                role="owner",
                allowed_project_ids=[],
            )
            await session.execute(
                text("SELECT set_config('app.current_org', :v, true)"),
                {"v": str(org_id)},
            )
            session.add(org)
            session.add(user)

    logger.info(
        "[AUTH] Bootstrapped org=%s user=%s email=%s",
        org.public_id,
        user.public_id,
        identity.email,
    )
    return BootstrapResponse(
        user_id=user.public_id,
        org_id=org_id,
        email=identity.email,
        created=True,
    )
