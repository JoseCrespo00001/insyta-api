import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.core.config import Settings

_PROD_FERNET_KEY = Fernet.generate_key().decode()


def _settings(env: dict[str, str]) -> Settings:
    base = {}
    if env.get("environment") not in (None, "development"):
        base["webhook_secret_key"] = _PROD_FERNET_KEY
    base.update(env)
    return Settings(_env_file=None, **base)


class TestJwtSecretValidation:
    def test_weak_secret_in_production_raises(self):
        with pytest.raises(ValidationError):
            _settings({"environment": "production", "jwt_secret": "change-me"})

    def test_short_secret_in_production_raises(self):
        with pytest.raises(ValidationError):
            _settings({"environment": "production", "jwt_secret": "short"})

    def test_empty_secret_in_production_raises(self):
        with pytest.raises(ValidationError):
            _settings({"environment": "production", "jwt_secret": ""})

    def test_weak_secret_in_staging_raises(self):
        with pytest.raises(ValidationError):
            _settings({"environment": "staging", "jwt_secret": "secret"})

    def test_strong_secret_in_production_passes(self):
        s = _settings({"environment": "production", "jwt_secret": "x" * 32})
        assert s.jwt_secret == "x" * 32

    def test_weak_secret_in_development_passes(self):
        s = _settings({"environment": "development", "jwt_secret": "change-me"})
        assert s.jwt_secret == "change-me"

    def test_short_secret_in_development_passes(self):
        s = _settings({"environment": "development", "jwt_secret": "x"})
        assert s.jwt_secret == "x"


class TestLifespanFailFast:
    def test_lifespan_raises_runtime_error_on_weak_secret_production(self):
        from app.core.config import WEAK_JWT_SECRETS

        for weak in WEAK_JWT_SECRETS:
            assert weak in WEAK_JWT_SECRETS
        assert "change-me" in WEAK_JWT_SECRETS


class TestWebhookSecretKeyValidation:
    def test_short_key_in_production_raises(self):
        with pytest.raises(ValidationError):
            _settings(
                {
                    "environment": "production",
                    "jwt_secret": "x" * 32,
                    "webhook_secret_key": "short",
                }
            )

    def test_empty_key_in_production_raises(self):
        with pytest.raises(ValidationError):
            _settings(
                {
                    "environment": "production",
                    "jwt_secret": "x" * 32,
                    "webhook_secret_key": "",
                }
            )

    def test_long_key_in_production_passes(self):
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        s = _settings(
            {
                "environment": "production",
                "jwt_secret": "x" * 32,
                "webhook_secret_key": key,
            }
        )
        assert s.webhook_secret_key.get_secret_value() == key

    def test_empty_key_in_development_passes(self):
        s = _settings(
            {
                "environment": "development",
                "webhook_secret_key": "",
            }
        )
        assert s.webhook_secret_key.get_secret_value() == ""
