from functools import lru_cache

from pydantic import Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

WEAK_JWT_SECRETS: frozenset[str] = frozenset({"change-me", "secret", "test", ""})
JWT_SECRET_MIN_LENGTH = 32
WEBHOOK_SECRET_KEY_MIN_LENGTH = 44


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: str = Field(default="development")
    log_level: str = Field(default="INFO")
    # B7: confianza mínima para que un VETO del LLM sea firme (topea el score).
    # Por debajo → VETO tentativo ("a confirmar"). Los DET (CBU/precio) son firmes.
    veto_confidence_threshold: float = Field(default=0.6)

    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5433/insyta"
    )
    # URL usada SOLO por Alembic (crea tablas/extensiones/policies → necesita el
    # rol dueño `postgres`). El runtime usa `database_url` con un rol sin BYPASSRLS
    # para que las policies RLS se ejerzan. Si no se setea, cae a `database_url`.
    migration_database_url: str | None = None
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

    webhook_secret_key: SecretStr = Field(default=SecretStr(""))

    cors_origins: str = Field(default="http://localhost:3000")

    phoenix_endpoint: str | None = None
    sentry_dsn: str | None = None

    resend_api_key: str | None = None
    email_from: str = Field(default="alerts@insyta.space")

    @field_validator("jwt_secret")
    @classmethod
    def validate_jwt_secret(cls, v: str, info: ValidationInfo) -> str:
        environment = info.data.get("environment", "development")
        if environment == "development":
            return v
        if v in WEAK_JWT_SECRETS:
            raise ValueError(
                f"jwt_secret cannot be a known weak value in environment={environment!r}"
            )
        if len(v) < JWT_SECRET_MIN_LENGTH:
            raise ValueError(
                f"jwt_secret must be at least {JWT_SECRET_MIN_LENGTH} characters "
                f"in environment={environment!r} (got {len(v)})"
            )
        return v

    @field_validator("webhook_secret_key")
    @classmethod
    def validate_webhook_secret_key(cls, v: SecretStr, info: ValidationInfo) -> SecretStr:
        # Los webhooks están diferidos (no hay feature de webhook), así que este
        # secreto es OPCIONAL: solo validamos el formato SI se provee un valor.
        # Cuando se implemente el slice de webhooks, hacerlo obligatorio ahí.
        # (El cifrado de secretos en DB NO usa esta key: deriva del jwt_secret,
        # ver services/secret_crypto.py.)
        environment = info.data.get("environment", "development")
        raw = v.get_secret_value()
        if environment == "development" or not raw:
            return v
        if len(raw) < WEBHOOK_SECRET_KEY_MIN_LENGTH:
            raise ValueError(
                "webhook_secret_key, si se setea, debe ser una Fernet key base64 "
                f"(>= {WEBHOOK_SECRET_KEY_MIN_LENGTH} chars) en "
                f"environment={environment!r} (got {len(raw)})"
            )
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
