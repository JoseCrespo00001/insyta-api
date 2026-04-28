"""Supabase JWT auth.

`get_current_user` validates a Bearer token against Supabase's JWKS endpoint
(cached for 1 hour), extracts identity + org claims, and returns a `CurrentUser`
DTO that downstream dependencies (notably `get_db_with_tenant_context`) consume.

Custom claims expected on the JWT:
    - sub        Supabase user id
    - email      verified email
    - org_id     uuid string of the user's organization
    - allowed_project_ids  optional comma-separated list (or list) of uuids

If `org_id` is absent we 403 — a token without an org cannot scope RLS.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from functools import lru_cache

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt
from jose.exceptions import JWTError

from app.core.config import get_settings

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)

_JWKS_TTL_SECONDS = 3600


@dataclass(frozen=True)
class CurrentUser:
    user_id: str
    email: str
    org_id: uuid.UUID
    allowed_project_ids: list[uuid.UUID]
    raw_claims: dict


@lru_cache(maxsize=1)
def _jwks_cache_state() -> dict:
    """Mutable holder so we can refresh inside the lru_cache slot."""
    return {"keys": None, "fetched_at": 0.0}


def _supabase_jwks_url() -> str:
    settings = get_settings()
    if not settings.supabase_url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SUPABASE_URL not configured",
        )
    return f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"


def _fetch_jwks() -> dict:
    state = _jwks_cache_state()
    now = time.monotonic()
    if state["keys"] is not None and (now - state["fetched_at"]) < _JWKS_TTL_SECONDS:
        return state["keys"]
    url = _supabase_jwks_url()
    response = httpx.get(url, timeout=5.0)
    response.raise_for_status()
    state["keys"] = response.json()
    state["fetched_at"] = now
    return state["keys"]


def _decode_with_supabase_secret(token: str) -> dict:
    """Decode legacy HS256 Supabase JWTs using the project secret.

    Supabase still issues HS256 tokens by default; JWKS asymmetric keys are
    opt-in. Try the JWT secret first since it's the common path.
    """
    settings = get_settings()
    secret = settings.supabase_service_key or settings.jwt_secret
    return jwt.decode(
        token,
        secret,
        algorithms=[settings.jwt_algorithm or "HS256"],
        options={"verify_aud": False},
    )


def _decode_with_jwks(token: str) -> dict:
    jwks = _fetch_jwks()
    headers = jwt.get_unverified_header(token)
    kid = headers.get("kid")
    key = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
    if key is None:
        raise JWTError("no matching JWK for kid")
    return jwt.decode(
        token,
        key,
        algorithms=[headers.get("alg", "RS256")],
        options={"verify_aud": False},
    )


def _decode_token(token: str) -> dict:
    try:
        return _decode_with_supabase_secret(token)
    except JWTError:
        try:
            return _decode_with_jwks(token)
        except (JWTError, HTTPException, httpx.HTTPError) as exc:
            # Re-raise as JWTError so the caller maps to 401, not 500.
            raise JWTError(str(exc)) from exc


def _parse_uuid_list(value: object) -> list[uuid.UUID]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [s.strip() for s in value.split(",") if s.strip()]
    elif isinstance(value, list):
        items = [str(s).strip() for s in value if s]
    else:
        return []
    out: list[uuid.UUID] = []
    for item in items:
        try:
            out.append(uuid.UUID(item))
        except ValueError:
            continue
    return out


async def get_current_user(token: str | None = Depends(oauth2_scheme)) -> CurrentUser:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = _decode_token(token)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user_id = claims.get("sub")
    email = claims.get("email")
    org_id_str = claims.get("org_id") or claims.get("app_metadata", {}).get("org_id")
    if not user_id or not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing required claims (sub/email)",
        )
    if not org_id_str:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token has no org_id claim — user not bound to an organization",
        )
    try:
        org_id = uuid.UUID(str(org_id_str))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token org_id is not a valid uuid",
        ) from exc

    allowed = _parse_uuid_list(
        claims.get("allowed_project_ids")
        or claims.get("app_metadata", {}).get("allowed_project_ids")
    )

    return CurrentUser(
        user_id=str(user_id),
        email=str(email),
        org_id=org_id,
        allowed_project_ids=allowed,
        raw_claims=claims,
    )
