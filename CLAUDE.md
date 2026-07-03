# insyta-api — backend (FastAPI + Celery + Postgres RLS)

Scoped guide. Cross-cutting rules + domain glossary are in the repo root
(`../CLAUDE.md`, `../CONTEXT.md`). This file is API-specific only.

## Stack

FastAPI · SQLAlchemy 2.0 (async, asyncpg) · Alembic · Celery[redis] · Supabase/Postgres
(RLS) · Pydantic v2 · Anthropic + OpenAI + **DeepSeek** · Presidio (PII) · OpenTelemetry →
Phoenix. Package manager: **uv** (`uv sync`, `uv run ...`). Python ≥3.11 (≥3.12 en prod).

## Cómo arranca / URL

- Local: `uv run uvicorn app.main:app --reload` → `http://127.0.0.1:8000`. Swagger en `/docs`,
  ReDoc en `/redoc` (no están deshabilitados).
- Docker: `uvicorn app.main:app --host 0.0.0.0 --port 8000` (`Dockerfile`).
- `create_app()` (`app/main.py`) arma la app, monta CORS y registra los routers. El `lifespan`
  valida que `jwt_secret` no sea débil fuera de `environment=development`.

## Layout (`app/`)

- `routers/` — endpoints HTTP. Thin: validan → llaman service. Pydantic schemas **inline** en
  cada router (con `alias_generator=to_camel`). Reales: `auth`, `me`, `health`, `projects`,
  `conversations`, `uploads`, `flows`, `audits`, `improvements`, `dashboard`, `settings`.
- `services/` — lógica de negocio: `anonymizer.py` (PII), `secret_crypto.py` (Fernet, deriva la
  key del `jwt_secret`), `celery_app.py`, `idempotency.py`, `report_format.py`, `uploads_storage.py`.
- `workers/` — Celery tasks: `processor.py` (parsea uploads → conversaciones/mensajes),
  `audit.py` (`run_audit`: LLM-as-judge sobre las conversaciones de una auditoría),
  `evaluator.py` (`evaluate_conversation`, eval por conversación), `parsers/` (WhatsApp/CSV/Respondio).
- `models/` — SQLAlchemy ORM (`tenancy.py`, `audits.py`, `flows.py`, `uploads.py`,
  `improvements.py`, `base.py` con mixins).
- `llm/` — integración LLM: `router.py` (`build_router`/`LLMRouter`, Anthropic/OpenAI/DeepSeek
  + retry), `audit_judge.py` (verdicts por mensaje), `flow_audit.py`, `credentials.py`
  (ContextVar para keys per-tenant), `schemas.py`, `prompts/`, `knowledge/`.
- `core/` — `config.py` (Settings Pydantic), `auth.py` (JWT/`get_current_user`),
  `db.py` (engine async + `get_db_with_tenant_context` + `tenant_txn`).
- `observability/` — spans Phoenix/OTel. `cli/` — comandos de utilidad.

> **No hay `schemas/` ni `webhooks/`** como capas separadas: los schemas Pydantic viven inline en
> los routers + `llm/schemas.py`, y no hay webhooks inbound implementados (ver "Estado actual").
> (Las carpetas vacías de scaffolding `app/schemas/` y `app/webhooks/` se eliminaron en el audit 2026-06-24.)

## Estado actual vs diseño diferido

El sistema HOY es **CSV-driven**: el usuario sube un archivo (`uploads`) → `processor` crea
conversaciones → el usuario dispara una **auditoría** (`audits`) → `run_audit` corre el judge y
produce evaluaciones, evaluaciones por mensaje, sugerencias y mejoras. **No hay** ingestión por
webhook, ni SSE/live-feed, ni alertas, ni retención, ni Celery Beat (`beat_schedule = {}` en
`services/celery_app.py`).

Los ADR 0001 (webhook granularity), 0003 (alert dedup), 0004 (SSE stateless) y 0005 (evaluator
publishes feed) describen ese diseño **todavía no implementado**. Trátalos como decisiones de
arquitectura futuras, no como código vivo. Si los implementás, seguí el ADR.

## Iron rules (API-specific)

- **Every DB session goes through `get_db_with_tenant_context`** — setea los GUCs
  `app.current_org` / `app.allowed_projects` que leen las policies RLS (vía `set_config(...,true)`
  con bound params). Una sesión cruda ve cero filas. Nunca `SET LOCAL` a mano en un router.
  Workers usan `tenant_txn(...)` para el mismo fin.
- **Anonymize before any LLM call.** `app.services.anonymizer` → tokens reversibles `[KIND_AABB]`.
  El sufijo de 4 chars es determinístico (cache-friendly) — no lo randomices.
- **Routers stay thin.** Nada de queries SQLAlchemy en routers; empujá a services/workers.
- **Pydantic v2 only** — `model_validate`, `model_dump`, `ConfigDict`. Nada de `.dict()` v1.
- One **Evaluation** per Conversation (`UNIQUE(conversation_id)`). Respetá los upserts idempotentes
  (`pg_insert(...).on_conflict_do_nothing`).
- **API keys de proveedor cifradas** con Fernet en `organizations.{provider}_api_key_encrypted`;
  nunca se devuelven en claro (la UI ve un masked). Ver `routers/settings.py` + `services/secret_crypto.py`.

## Migrations

Alembic. Archivos en `migrations/versions/*` con prefijo de fecha (ej.
`20260623_1200_deepseek_provider.py`). No edites a mano una migración ya shippeada. Cambio de schema:
`uv run alembic revision --autogenerate -m "..."` y revisá.

## Commands (run from `insyta-api/`)

```bash
uv sync                          # install
uv run uvicorn app.main:app --reload   # http://127.0.0.1:8000  (/docs, /redoc)
uv run pytest                    # tests scoped here (testpaths=["tests"], asyncio auto)
uv run ruff check . && uv run ruff format .
uv run mypy app
uv run python scripts/audit_bpmn_coverage.py   # BPMN audit: endpoints vs BPMN tasks
```

Run tests from **this dir**, not repo root, to keep them scoped and avoid timeouts.
ruff line-length = 100. mypy en strict.

## Soft-delete (regla dura)

- **Nunca `DROP`/`TRUNCATE`/reset de la base ni de una tabla.** La DB de prod no se borra ni se
  reinicia jamás. Migraciones **solo aditivas/reversibles** (no destructivas).
- **Delete = soft-delete.** Los endpoints DELETE setean `is_deleted=True` (+ `deleted_at`), nunca
  borran filas. Cascada replicada en `app/services/soft_delete.py` (`soft_delete_project/_conversations/_upload/_flow`).
- **Toda lectura filtra `is_deleted = False`.** Se aplica global con un evento `do_orm_execute` +
  `with_loader_criteria` en `app/core/db.py` (cubre selects de entidad, agregados y outer joins).
  Para ver filas borradas a propósito (upsert idempotente, el propio soft-delete, mantenimiento):
  `.execution_options(include_deleted=True)`.
- Modelos borrables llevan `SoftDeleteMixin` (`app/models/base.py`). `Organization`/`User` NO.

## Uploads (storage)

- El CSV subido va a **Supabase Storage** (bucket privado `uploads`), no a disco local. Helpers en
  `app/services/uploads_storage.py` (`write_upload`/`read_upload`/`delete_upload_blob`). El worker lo
  baja por la object key en `uploads.storage_path`. Requiere `SUPABASE_URL` + `SUPABASE_SERVICE_KEY`.

## Don'ts

- Don't bypass RLS, don't log raw PII.
- Don't call Anthropic/OpenAI/DeepSeek outside `app/llm/` clients.
- Don't reintroducir webhooks/SSE/alerts/Beat ad hoc — si los traés, es un vertical slice que
  sigue su ADR (0001/0003/0004/0005).
- Don't borrar/reiniciar la DB ni escribir migraciones destructivas (ver Soft-delete arriba).
- Don't volver a persistir uploads en disco local (usar Supabase Storage).
