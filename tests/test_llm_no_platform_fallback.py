"""Slice A del funnel self-serve: una org SIN API key propia NO puede gastar la
key de plataforma del `.env`.

Cubre:
  - credentials.py sin fallback: los getters devuelven SOLO el override del
    ContextVar (None aunque el entorno tenga la key cargada);
  - guard 402 en POST /projects/{id}/audits (fail-fast antes de encolar);
  - happy-path 202 cuando la org SÍ tiene key (send_task mockeado);
  - rate limit por IP en POST audits (10/min → el 11º da 429);
  - defensa en profundidad en el worker: `_run` falla con FatalLLMError claro
    si la org no tiene key del motor elegido (run_audit lo marca "failed").
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.db import get_db_with_tenant_context
from app.llm import credentials
from app.llm.router import FatalLLMError
from app.main import app
from app.services.secret_crypto import encrypt_secret
from app.workers import audit as audit_worker

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/insyta",
)


# ---------------------------------------------------------------------------
# credentials.py: sin fallback a la key de plataforma del entorno.
# Los tests son async a propósito: cada uno corre en su propia Task (contexto
# de ContextVars copiado), así el set de un test no contamina al siguiente.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_getters_ignoran_la_key_de_plataforma_del_env(monkeypatch):
    """Aunque el entorno tenga las keys de plataforma cargadas, los getters
    devuelven None si la org no seteó la suya (no hay fallback)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-plataforma-NO-usar")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-plataforma-NO-usar")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-plataforma-NO-usar")
    get_settings.cache_clear()
    try:
        assert credentials.get_anthropic_key() is None
        assert credentials.get_openai_key() is None
        assert credentials.get_deepseek_key() is None
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_set_llm_keys_es_la_unica_fuente():
    credentials.set_llm_keys(deepseek="sk-org-propia-deepseek")
    assert credentials.get_deepseek_key() == "sk-org-propia-deepseek"
    # Los otros providers siguen sin key (no se setearon).
    assert credentials.get_anthropic_key() is None


# ---------------------------------------------------------------------------
# Helpers de DB (mismo patrón que test_audit_worker / test_settings_endpoint).
# ---------------------------------------------------------------------------
async def _db_available() -> bool:
    eng = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with eng.connect() as c:
            await c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        await eng.dispose()


async def _seed_org_project_conv(
    eng: AsyncEngine,
    *,
    org_id: uuid.UUID,
    proj_id: uuid.UUID,
    with_anthropic_key: bool,
) -> str:
    """Org (+/- key cifrada) + project + agent + conversation. Devuelve el
    public_id de la conversación (para el payload del POST)."""
    agent_id = uuid.uuid4()
    conv_id = uuid.uuid4()
    factory = async_sessionmaker(eng, expire_on_commit=False)
    async with factory() as s, s.begin():
        await s.execute(
            text(
                "INSERT INTO organizations"
                "(id, public_id, slug, name, anthropic_api_key_encrypted) "
                "VALUES (:id, :pid, :slug, 'Org', :akey)"
            ),
            {
                "id": org_id,
                "pid": f"org_{org_id.hex[:16]}",
                "slug": f"o-{org_id.hex[:6]}",
                "akey": (
                    encrypt_secret("sk-ant-test-org-key-000000000000")
                    if with_anthropic_key
                    else None
                ),
            },
        )
        await s.execute(
            text(
                "INSERT INTO projects(id, public_id, org_id, slug, name) "
                "VALUES (:id, :pid, :org, :slug, 'P')"
            ),
            {
                "id": proj_id,
                "pid": f"proj_{proj_id.hex[:16]}",
                "org": org_id,
                "slug": f"p-{proj_id.hex[:6]}",
            },
        )
        await s.execute(
            text(
                "INSERT INTO agents(id, public_id, project_id, org_id, slug, name, platform) "
                "VALUES (:id, :pid, :proj, :org, 'default', 'A', 'custom_sdk')"
            ),
            {
                "id": agent_id,
                "pid": f"agt_{agent_id.hex[:16]}",
                "proj": proj_id,
                "org": org_id,
            },
        )
        await s.execute(
            text(
                "INSERT INTO conversations(id, public_id, project_id, org_id, agent_id, "
                "external_id, platform, message_count, status) "
                "VALUES (:id, :pid, :proj, :org, :agent, 'ext-1', 'custom_sdk', 1, 'completed')"
            ),
            {
                "id": conv_id,
                "pid": f"conv_{conv_id.hex[:16]}",
                "proj": proj_id,
                "org": org_id,
                "agent": agent_id,
            },
        )
    return f"conv_{conv_id.hex[:16]}"


async def _cleanup_org(eng: AsyncEngine, org_id: uuid.UUID) -> None:
    factory = async_sessionmaker(eng, expire_on_commit=False)
    async with factory() as s, s.begin():
        await s.execute(
            text("DELETE FROM organizations WHERE id = :id"), {"id": org_id}
        )


def _override_session(eng: AsyncEngine, org_id: uuid.UUID):
    """Sesión superuser con el GUC de tenant seteado (bypassa auth: el override
    reemplaza a get_db_with_tenant_context y con él a get_current_user)."""
    factory = async_sessionmaker(eng, expire_on_commit=False)

    async def _override():
        async with factory() as s:
            async with s.begin():
                await s.execute(
                    text("SELECT set_config('app.current_org', :v, true)"),
                    {"v": str(org_id)},
                )
                await s.execute(
                    text("SELECT set_config('app.allowed_projects', :v, true)"),
                    {"v": ""},
                )
                yield s

    return _override


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# POST /projects/{id}/audits — guard 402 / happy path / rate limit.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_post_audits_sin_key_de_org_devuelve_402():
    if not await _db_available():
        pytest.skip("DB unavailable")
    eng = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    org_id, proj_id = uuid.uuid4(), uuid.uuid4()
    try:
        conv_pid = await _seed_org_project_conv(
            eng, org_id=org_id, proj_id=proj_id, with_anthropic_key=False
        )
        app.dependency_overrides[get_db_with_tenant_context] = _override_session(
            eng, org_id
        )
        async with _client() as c:
            r = await c.post(
                f"/api/v1/projects/proj_{proj_id.hex[:16]}/audits",
                json={"conversationIds": [conv_pid]},
            )
        assert r.status_code == 402, r.text
        assert "API key de Anthropic" in r.json()["detail"]
        assert "Configuración" in r.json()["detail"]

        # Fail-fast: no quedó ningún audit encolado/creado.
        factory = async_sessionmaker(eng, expire_on_commit=False)
        async with factory() as s:
            n = (
                await s.execute(
                    text("SELECT count(*) FROM audits WHERE project_id = :p"),
                    {"p": proj_id},
                )
            ).scalar_one()
        assert n == 0
    finally:
        app.dependency_overrides.clear()
        await _cleanup_org(eng, org_id)
        await eng.dispose()


@pytest.mark.asyncio
async def test_post_audits_con_key_de_org_encola_202(monkeypatch):
    if not await _db_available():
        pytest.skip("DB unavailable")
    sent: list[tuple] = []
    monkeypatch.setattr(
        "app.routers.audits.celery_app.send_task",
        lambda name, args=None, **kw: sent.append((name, args)),
    )
    eng = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    org_id, proj_id = uuid.uuid4(), uuid.uuid4()
    try:
        conv_pid = await _seed_org_project_conv(
            eng, org_id=org_id, proj_id=proj_id, with_anthropic_key=True
        )
        app.dependency_overrides[get_db_with_tenant_context] = _override_session(
            eng, org_id
        )
        async with _client() as c:
            r = await c.post(
                f"/api/v1/projects/proj_{proj_id.hex[:16]}/audits",
                json={"conversationIds": [conv_pid]},
            )
        assert r.status_code == 202, r.text
        assert r.json()["status"] == "running"
        assert len(sent) == 1 and sent[0][0] == "app.workers.audit.run_audit"
    finally:
        app.dependency_overrides.clear()
        await _cleanup_org(eng, org_id)
        await eng.dispose()


@pytest.mark.asyncio
async def test_post_audits_rate_limit_11_en_un_minuto_devuelve_429(monkeypatch):
    if not await _db_available():
        pytest.skip("DB unavailable")
    monkeypatch.setattr(
        "app.routers.audits.celery_app.send_task", lambda *a, **kw: None
    )
    eng = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    org_id, proj_id = uuid.uuid4(), uuid.uuid4()
    try:
        conv_pid = await _seed_org_project_conv(
            eng, org_id=org_id, proj_id=proj_id, with_anthropic_key=True
        )
        app.dependency_overrides[get_db_with_tenant_context] = _override_session(
            eng, org_id
        )
        statuses = []
        async with _client() as c:
            for _ in range(11):
                r = await c.post(
                    f"/api/v1/projects/proj_{proj_id.hex[:16]}/audits",
                    json={"conversationIds": [conv_pid]},
                )
                statuses.append(r.status_code)
        assert statuses[:10] == [202] * 10
        assert statuses[10] == 429
        assert "Demasiadas solicitudes" in r.json()["detail"]
    finally:
        app.dependency_overrides.clear()
        await _cleanup_org(eng, org_id)
        await eng.dispose()


# ---------------------------------------------------------------------------
# Worker: defensa en profundidad — sin key de la org, _run falla claro.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_worker_run_sin_key_de_org_falla_sin_tocar_la_key_de_plataforma(
    monkeypatch,
):
    if not await _db_available():
        pytest.skip("DB unavailable")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    # Aunque el entorno tenga la key de plataforma, el worker NO debe usarla.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-plataforma-NO-usar")
    get_settings.cache_clear()

    eng = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    org_id, proj_id = uuid.uuid4(), uuid.uuid4()
    audit_id = uuid.uuid4()
    try:
        conv_pid = await _seed_org_project_conv(
            eng, org_id=org_id, proj_id=proj_id, with_anthropic_key=False
        )
        factory = async_sessionmaker(eng, expire_on_commit=False)
        async with factory() as s, s.begin():
            conv_id = (
                await s.execute(
                    text("SELECT id FROM conversations WHERE public_id = :p"),
                    {"p": conv_pid},
                )
            ).scalar_one()
            await s.execute(
                text(
                    "INSERT INTO audits(id, public_id, project_id, org_id, name, status, "
                    "conversation_count) "
                    "VALUES (:id, :pid, :proj, :org, 'Audit', 'running', 1)"
                ),
                {
                    "id": audit_id,
                    "pid": f"aud_{audit_id.hex[:16]}",
                    "proj": proj_id,
                    "org": org_id,
                },
            )
            await s.execute(
                text(
                    "INSERT INTO audit_conversations(id, audit_id, conversation_id, "
                    "project_id, org_id) VALUES (:id, :aud, :conv, :proj, :org)"
                ),
                {
                    "id": uuid.uuid4(),
                    "aud": audit_id,
                    "conv": conv_id,
                    "proj": proj_id,
                    "org": org_id,
                },
            )

        with pytest.raises(FatalLLMError) as exc_info:
            await audit_worker._run(audit_id, org_id)
        # Error claro y accionable (es lo que run_audit persiste en error_message).
        assert "API key de Anthropic" in str(exc_info.value)
    finally:
        get_settings.cache_clear()
        await _cleanup_org(eng, org_id)
        await eng.dispose()
