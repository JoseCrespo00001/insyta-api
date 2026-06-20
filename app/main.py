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
    uploads,
)

logger = logging.getLogger(__name__)


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
    app.include_router(uploads.router)
    app.include_router(flows.router)
    app.include_router(audits.router)
    app.include_router(improvements.router)
    app.include_router(dashboard.router)

    return app


app = create_app()
