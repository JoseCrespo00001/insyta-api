# insyta-api

FastAPI backend para Insyta — plataforma de mejora continua de agentes LLM en produccion.

## Stack

- **Python 3.12+** con `uv` para deps
- **FastAPI 0.115+** async
- **PostgreSQL 16** via Supabase (con RLS multi-tenant)
- **Celery + Redis** para workers async
- **Anthropic / OpenAI / DeepSeek** via LLM router
- **Presidio** para anonimizacion PII (NER local)

## Quickstart

```bash
# 1. Instalar deps
uv sync

# 2. Levantar PostgreSQL + Redis locales
docker compose up -d

# 3. Configurar env
cp .env.example .env
# editar .env con tus keys

# 4. Correr migraciones (cuando existan)
# alembic upgrade head

# 5. Levantar API
uv run uvicorn app.main:app --reload --port 8000

# 6. Verificar
curl http://localhost:8000/health
# -> {"status":"ok","version":"0.1.0"}
```

## Estructura

```
app/
├── core/         Config, database, security, dependencies
├── models/       SQLAlchemy ORM
├── schemas/      Pydantic v2 request/response
├── routers/      Endpoints
├── services/     Business logic
├── workers/      Celery tasks
├── llm/          LLM provider abstraction
└── webhooks/     Webhook handlers (WATI, Respond.io, ...)
```

## Tests

```bash
uv run pytest
uv run pytest --cov=app
```

## Lint / format

```bash
uv run ruff check .
uv run ruff format .
```

## Pre-commit hooks

```bash
# 1. Instalar pre-commit (una sola vez)
uv tool install pre-commit  # o: pipx install pre-commit

# 2. Activar hooks en este repo
pre-commit install

# 3. Correr todos los hooks contra el repo entero (opcional)
pre-commit run --all-files
```

Los hooks corren ruff (check + format) y mypy en cada commit.

## Convencion de commits

Para que el git hook sincronice con Linear, usa:

```
[INSYTA-N] descripcion del cambio        # comenta en issue
closes INSYTA-N: descripcion              # cierra issue
[wip INSYTA-N] descripcion                # marca en progreso
```

Ejemplo: `[INSYTA-2] add Supabase RLS policies for tenants table`
