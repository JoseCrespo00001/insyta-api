import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import WEAK_JWT_SECRETS, get_settings
from app.routers import (
    audits,
    auth,
    clients,
    conversations,
    dashboard,
    flows,
    health,
    improvements,
    me,
    notifications,
    projects,
    score,
    supervisors,
    uploads,
)
from app.routers import (
    settings as settings_router,
)

logger = logging.getLogger(__name__)

# Orden + descripción de los grupos en Swagger/ReDoc. FastAPI respeta este orden
# y muestra la descripción bajo cada sección, así los endpoints son fáciles de
# ubicar (en vez de salir alfabéticos y sin contexto).
OPENAPI_TAGS = [
    {"name": "health", "description": "Liveness/readiness. Sin auth."},
    {
        "name": "auth",
        "description": "Bootstrap del usuario+org en el primer hit (Supabase JWT).",
    },
    {
        "name": "me",
        "description": "Identidad del usuario actual (org, rol, proyectos permitidos).",
    },
    {"name": "projects", "description": "CRUD de proyectos del tenant."},
    {
        "name": "conversations",
        "description": "Lectura de conversaciones/mensajes (lista, detalle, borrado).",
    },
    {
        "name": "score",
        "description": "Score agregado (0-100) por proyecto a partir de las evaluaciones.",
    },
    {
        "name": "uploads",
        "description": "Subida de CSV/export → ingestión (procesa a conversaciones).",
    },
    {
        "name": "flows",
        "description": "Flujos (Langflow): alta, listado y auditoría del flujo.",
    },
    {
        "name": "audits",
        "description": "Auditorías: dispara el LLM-as-judge sobre conversaciones.",
    },
    {
        "name": "improvements",
        "description": "Mejoras sugeridas a partir de las auditorías.",
    },
    {"name": "dashboard", "description": "Métricas agregadas del tenant."},
    {"name": "settings", "description": "API keys de proveedor LLM (cifradas Fernet)."},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.environment != "development" and settings.jwt_secret in WEAK_JWT_SECRETS:
        raise RuntimeError(
            f"JWT secret not configured (got weak default in environment={settings.environment!r})"
        )

    # Guard anti-bypass de RLS: si el rol con el que conectamos a Postgres es
    # superuser o tiene BYPASSRLS, las policies de multi-tenancy quedan inertes
    # (FORCE ROW LEVEL SECURITY no aplica a esos roles) y cualquier usuario ve la
    # data de todas las orgs. Fuera de development —o apuntando a una DB de prod
    # aunque environment diga development— nos negamos a arrancar.
    from sqlalchemy import text

    from app.core.db import engine

    async with engine.connect() as conn:
        role = (
            await conn.execute(
                text(
                    "SELECT current_user AS name, rolsuper, rolbypassrls "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()
    privileged = bool(role.rolsuper or role.rolbypassrls)
    is_prod_db = any(
        marker in settings.database_url
        for marker in settings.prod_db_host_markers.split(",")
        if marker.strip()
    )
    if privileged and (settings.environment != "development" or is_prod_db):
        raise RuntimeError(
            f"Refusing to start: DB role {role.name!r} bypasses RLS "
            f"(rolsuper={role.rolsuper}, rolbypassrls={role.rolbypassrls}). "
            "Connect as the non-privileged app role (insyta_app)."
        )
    if privileged:
        logger.warning(
            "[STARTUP] DB role %r bypasses RLS — OK solo en DB local descartable.",
            role.name,
        )

    # Tracing OTel→Phoenix desactivado por ahora. Para reactivar, descomentar:
    # from app.observability import init_tracing
    #
    # init_tracing()  # idempotente; NoOp si PHOENIX_ENDPOINT vacío
    logger.info("[STARTUP] Insyta API starting in %s mode", settings.environment)
    yield
    logger.info("[SHUTDOWN] Insyta API shutting down")


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Hook global de errores: toda excepción NO manejada (un bug, un fallo de
    DB, etc.) se logea con el prefijo [UNHANDLED] + contexto (método/path/tipo)
    para identificarla, y devuelve un 500 limpio sin filtrar detalles internos.
    Las HTTPException (4xx intencionales) las maneja FastAPI aparte."""
    logger.exception(
        "[UNHANDLED] %s %s -> %s: %s",
        request.method,
        request.url.path,
        type(exc).__name__,
        exc,
    )
    return JSONResponse(status_code=500, content={"detail": "Error interno del servidor"})


def create_app() -> FastAPI:
    settings = get_settings()

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    app = FastAPI(
        title="Insyta API",
        version="0.1.0",
        description="Plataforma de mejora continua de agentes LLM en produccion",
        lifespan=lifespan,
        openapi_tags=OPENAPI_TAGS,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Hook global de acceso: logea cada request con prefijo [REQ] + duración, así
    # se puede seguir qué pega a qué endpoint sin instrumentar cada uno a mano.
    @app.middleware("http")
    async def _log_requests(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        if request.url.path != "/health":  # /health lo pollea el LB, no ensuciar
            dur_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "[REQ] %s %s -> %d (%.0fms)",
                request.method,
                request.url.path,
                response.status_code,
                dur_ms,
            )
        return response

    # Hook global de errores (ver _unhandled_exception_handler).
    app.add_exception_handler(Exception, _unhandled_exception_handler)

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(me.router)
    app.include_router(projects.router)
    app.include_router(conversations.router)
    app.include_router(score.router)
    app.include_router(uploads.router)
    app.include_router(flows.router)
    app.include_router(supervisors.router)
    app.include_router(audits.router)
    app.include_router(clients.router)
    app.include_router(improvements.router)
    app.include_router(notifications.router)
    app.include_router(dashboard.router)
    app.include_router(settings_router.router)

    return app


app = create_app()
