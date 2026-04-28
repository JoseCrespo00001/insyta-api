import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _settings(env: dict[str, str]) -> Settings:
    return Settings(_env_file=None, **env)


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
