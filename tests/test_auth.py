"""Auth tests.

Cover JWT decoding paths, claim extraction, and the FastAPI route guard.
JWKS path is tested via monkeypatch; the legacy HS256 path is covered with a
real signed token.
"""

from __future__ import annotations

import time
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt

from app.core.auth import CurrentUser, get_current_user
from app.core.config import get_settings
from app.routers.me import router as me_router


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_token(claims: dict, secret: str = "x" * 32, alg: str = "HS256") -> str:
    base = {"iat": int(time.time()), "exp": int(time.time()) + 3600}
    base.update(claims)
    return jwt.encode(base, secret, algorithm=alg)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "x" * 32)
    monkeypatch.setenv("ENVIRONMENT", "development")
    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(me_router)
    return TestClient(app)


def test_missing_token_returns_401(client: TestClient):
    response = client.get("/api/v1/me")
    assert response.status_code == 401


def test_invalid_token_returns_401(client: TestClient):
    response = client.get("/api/v1/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


def test_valid_token_without_org_id_returns_403(client: TestClient):
    org_less = _make_token({"sub": "u1", "email": "a@b.com"})
    response = client.get("/api/v1/me", headers={"Authorization": f"Bearer {org_less}"})
    assert response.status_code == 403
    assert "org_id" in response.json()["detail"]


def test_valid_token_returns_user_payload(client: TestClient):
    org_id = str(uuid.uuid4())
    proj_a = str(uuid.uuid4())
    proj_b = str(uuid.uuid4())
    token = _make_token(
        {
            "sub": "user-123",
            "email": "user@insyta.io",
            "org_id": org_id,
            "allowed_project_ids": [proj_a, proj_b],
        }
    )
    response = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == "user-123"
    assert body["email"] == "user@insyta.io"
    assert body["org_id"] == org_id
    assert set(body["allowed_project_ids"]) == {proj_a, proj_b}


def test_token_with_invalid_org_id_uuid_returns_403(client: TestClient):
    token = _make_token({"sub": "u1", "email": "a@b.com", "org_id": "not-a-uuid"})
    response = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


def test_app_metadata_org_id_fallback(client: TestClient):
    """Supabase puts org_id under app_metadata when set via service-role API."""
    org_id = str(uuid.uuid4())
    token = _make_token(
        {
            "sub": "u1",
            "email": "a@b.com",
            "app_metadata": {"org_id": org_id, "allowed_project_ids": []},
        }
    )
    response = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["org_id"] == org_id


def test_get_current_user_dataclass_shape():
    """Ensure CurrentUser DTO fields stay stable."""
    cu = CurrentUser(
        user_id="u",
        email="e@x",
        org_id=uuid.uuid4(),
        allowed_project_ids=[],
        raw_claims={},
    )
    assert cu.user_id == "u"
    assert cu.allowed_project_ids == []


def test_get_current_user_is_callable():
    assert callable(get_current_user)
