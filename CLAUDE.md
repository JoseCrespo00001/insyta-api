# insyta-api — backend (FastAPI + Celery + Postgres RLS)

Scoped guide. Cross-cutting rules + domain glossary are in the repo root
(`../CLAUDE.md`, `../CONTEXT.md`). This file is API-specific only.

## Stack

FastAPI · SQLAlchemy 2.0 (async, asyncpg) · Alembic · Celery[redis] · Supabase/Postgres
(RLS) · Pydantic v2 · Anthropic + OpenAI · Presidio (PII) · structlog · OpenTelemetry →
Phoenix. Package manager: **uv** (`uv sync`, `uv run ...`). Python ≥3.11.

## Layout (`app/`)

- `routers/` — HTTP endpoints. Each maps to a BPMN task. Thin: validate → call service.
- `services/` — business logic. `anonymizer.py` (PII), `webhook_secret.py` (Fernet).
- `workers/` — Celery tasks: `evaluator.py` (LLM-as-judge), `processor.py`,
  `webhook_processor.py`, `alerts.py`, `retention.py`, `scheduler.py` (Beat), `parsers/`.
- `models/` — SQLAlchemy. `schemas/` — Pydantic I/O. `llm/` — provider clients.
- `core/` — config, deps (incl. `get_db_with_tenant_context`). `webhooks/` — inbound.
- `observability/` — Phoenix/OTel spans.

## Iron rules (API-specific)

- **Every DB session goes through `get_db_with_tenant_context`** — it sets the
  `app.current_org` / `app.allowed_projects` GUCs that RLS policies read. A raw session sees
  zero rows. Never `SET LOCAL` by hand in a router.
- **Anonymize before any LLM call.** `app.services.anonymizer` → reversible `[KIND_AABB]`
  tokens. The 4-char suffix is deterministic (cache-hit friendly) — don't randomize it.
- **Evaluator publish path** (ADR 0005): the 3-line block after `persist_evaluation` in
  `workers/evaluator.py` publishes to the tenant SSE channel + enqueues alert checks. There
  is NO Beat fanout task — don't reintroduce one.
- **Routers stay thin.** No SQLAlchemy queries in routers; push to services.
- **Pydantic v2 only** — `model_validate`, `model_dump`, `ConfigDict`. No v1 `.dict()`.
- One **Evaluation** per Conversation (`UNIQUE(conversation_id)`). Respect idempotent upserts.

## Migrations

Alembic. Never hand-edit `migrations/versions/*` after they ship. New schema change:
`uv run alembic revision --autogenerate -m "..."` then review. Migration 0003 encrypts
`webhook_secret` (Fernet) — plaintext columns are gone.

## Commands (run from `insyta-api/`)

```bash
uv sync                          # install
uv run uvicorn app.main:app --reload
uv run pytest                    # tests scoped here (testpaths=["tests"], asyncio auto)
uv run ruff check . && uv run ruff format .
uv run mypy app
uv run python scripts/audit_bpmn_coverage.py   # BPMN audit: endpoints vs BPMN tasks
```

Run tests from **this dir**, not repo root, to keep them scoped and avoid timeouts.
ruff line-length = 100.

## Don'ts

- Don't bypass RLS, don't log raw PII (structlog config strips it — keep it that way).
- Don't call Anthropic/OpenAI outside `app/llm/` clients.
- Don't add WebSockets — the live feed is SSE over Redis Pub/Sub (`tenant:{org_id}:feed`),
  ADR 0004.
