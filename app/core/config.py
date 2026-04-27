from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: str = Field(default="development")
    log_level: str = Field(default="INFO")

    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5433/insyta"
    )
    supabase_url: str | None = None
    supabase_anon_key: str | None = None
    supabase_service_key: str | None = None

    redis_url: str = Field(default="redis://localhost:6380/0")
    celery_broker_url: str = Field(default="redis://localhost:6380/1")
    celery_result_backend: str = Field(default="redis://localhost:6380/2")

    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    deepseek_api_key: str | None = None

    jwt_secret: str = Field(default="change-me")
    jwt_algorithm: str = Field(default="HS256")
    jwt_expiration_minutes: int = Field(default=60)

    cors_origins: str = Field(default="http://localhost:3000")

    phoenix_endpoint: str | None = None
    sentry_dsn: str | None = None

    resend_api_key: str | None = None
    email_from: str = Field(default="alerts@insyta.io")

    @property
    def cors_origin_list(self) -> list[str]:
        return [
            origin.strip() for origin in self.cors_origins.split(",") if origin.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
