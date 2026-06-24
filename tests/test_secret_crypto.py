"""Tests para services/secret_crypto.py — cifrado Fernet de API keys de proveedor.

Módulo de seguridad que estaba 0% cubierto (audit 2026-06-24). Prueba comportamiento,
no implementación: roundtrip, fallo silencioso ante tokens corruptos, y masking.
"""

from __future__ import annotations

import pytest

from app.services import secret_crypto
from app.services.secret_crypto import (
    decrypt_secret,
    encrypt_secret,
    mask_secret,
)


@pytest.fixture(autouse=True)
def _reset_fernet_cache():
    """`_fernet()` es lru_cached; limpiarlo aísla tests que tocan jwt_secret."""
    secret_crypto._fernet.cache_clear()
    yield
    secret_crypto._fernet.cache_clear()


class TestRoundtrip:
    def test_encrypt_then_decrypt_returns_original(self):
        plaintext = "sk-ant-api03-supersecret-value"
        token = encrypt_secret(plaintext)
        assert decrypt_secret(token) == plaintext

    def test_ciphertext_is_not_plaintext(self):
        plaintext = "sk-ant-api03-supersecret-value"
        token = encrypt_secret(plaintext)
        assert plaintext not in token

    def test_two_encryptions_differ_but_both_decrypt(self):
        """Fernet incluye un IV aleatorio → ciphertexts distintos, mismo plaintext."""
        plaintext = "sk-proj-abc123"
        a = encrypt_secret(plaintext)
        b = encrypt_secret(plaintext)
        assert a != b
        assert decrypt_secret(a) == plaintext
        assert decrypt_secret(b) == plaintext

    def test_empty_string_roundtrips(self):
        token = encrypt_secret("")
        assert decrypt_secret(token) == ""


class TestDecryptFailsSafe:
    def test_garbage_token_returns_none_not_crash(self):
        assert decrypt_secret("garbage-not-a-fernet-token") is None

    def test_empty_token_returns_none(self):
        assert decrypt_secret("") is None

    def test_tampered_token_returns_none(self):
        token = encrypt_secret("sk-ant-real")
        tampered = token[:-4] + "AAAA"
        assert decrypt_secret(tampered) is None


class TestKeyDerivation:
    def test_key_derives_from_jwt_secret(self, monkeypatch):
        """Cambiar el jwt_secret cambia la key: un token cifrado con un secret
        NO puede desencriptarse con otro. Defiende la propiedad de derivación."""
        from app.core import config

        # Cifrar bajo el secret actual.
        token = encrypt_secret("sk-ant-real")

        # Forzar un jwt_secret distinto y limpiar caches.
        config.get_settings.cache_clear()
        secret_crypto._fernet.cache_clear()
        monkeypatch.setattr(
            config.get_settings(),
            "jwt_secret",
            "un-secret-completamente-distinto-de-32+chars-largo",
            raising=False,
        )
        secret_crypto._fernet.cache_clear()

        # Con otra key, el token viejo no se puede leer → None (fail-safe).
        assert decrypt_secret(token) is None

        # Restaurar para no contaminar otros tests.
        config.get_settings.cache_clear()
        secret_crypto._fernet.cache_clear()


class TestMask:
    def test_masks_long_secret(self):
        masked = mask_secret("sk-ant-api03-1234567890")
        assert masked.startswith("sk-ant")
        assert masked.endswith("7890")
        assert "1234567890" not in masked

    def test_short_secret_fully_masked(self):
        assert mask_secret("short") == "••••"

    def test_boundary_eight_chars_fully_masked(self):
        assert mask_secret("12345678") == "••••"
