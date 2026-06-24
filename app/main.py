import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import WEAK_JWT_SECRETS, get_settings
from app.routers import (
    audits,
    auth,
    conversations,
    dashboard,
    flows,
    health,
    improvements,
    me,
    projects,
    score,
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
    if (
        settings.environment != "development"
        and settings.jwt_secret in WEAK_JWT_SECRETS
    ):
        raise RuntimeError(
            f"JWT secret not configured (got weak default in environment={settings.environment!r})"
        )
    logger.info("[STARTUP] Insyta API starting in %s mode", settings.environment)
    yield
    logger.info("[SHUTDOWN] Insyta API shutting down")


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

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(me.router)
    app.include_router(projects.router)
    app.include_router(conversations.router)
    app.include_router(score.router)
    app.include_router(uploads.router)
    app.include_router(flows.router)
    app.include_router(audits.router)
    app.include_router(improvements.router)
    app.include_router(dashboard.router)
    app.include_router(settings_router.router)

    return app


app = create_app()
