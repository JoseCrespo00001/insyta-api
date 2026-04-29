"""Fernet round-trip + key-rotation tests for the webhook_secret helper."""

from __future__ import annotations

import os

import pytest
from cryptography.fernet import Fernet, InvalidToken

_DEFAULT_KEY = Fernet.generate_key().decode()
os.environ.setdefault("WEBHOOK_SECRET_KEY", _DEFAULT_KEY)

from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.services import webhook_secret  # noqa: E402

webhook_secret._fernet.cache_clear()


@pytest.fixture(autouse=True)
def _cache_reset():
    yield
    get_settings.cache_clear()
    webhook_secret._fernet.cache_clear()


def test_round_trip_recovers_original():
    raw = webhook_secret.generate_raw_webhook_secret()
    assert len(raw) >= 40
    encrypted = webhook_secret.encrypt_webhook_secret(raw)
    assert encrypted != raw.encode()
    decrypted = webhook_secret.decrypt_webhook_secret(encrypted)
    assert decrypted == raw


def test_round_trip_50_distinct_secrets():
    raws = [webhook_secret.generate_raw_webhook_secret() for _ in range(50)]
    assert len(set(raws)) == 50
    for r in raws:
        assert (
            webhook_secret.decrypt_webhook_secret(
                webhook_secret.encrypt_webhook_secret(r)
            )
            == r
        )


def test_decrypt_with_different_key_fails(monkeypatch):
    raw = "secret-original"
    encrypted = webhook_secret.encrypt_webhook_secret(raw)

    monkeypatch.setenv("WEBHOOK_SECRET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    webhook_secret._fernet.cache_clear()

    with pytest.raises(RuntimeError):
        webhook_secret.decrypt_webhook_secret(encrypted)


def test_tampered_ciphertext_fails():
    raw = "another-secret"
    encrypted = webhook_secret.encrypt_webhook_secret(raw)
    tampered = encrypted[:-1] + bytes([encrypted[-1] ^ 0x01])
    with pytest.raises(RuntimeError):
        webhook_secret.decrypt_webhook_secret(tampered)


def test_generate_raw_webhook_secret_is_url_safe():
    raw = webhook_secret.generate_raw_webhook_secret()
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    assert set(raw).issubset(allowed)


def test_decrypt_junk_bytes_raises_runtime_error():
    with pytest.raises(RuntimeError):
        webhook_secret.decrypt_webhook_secret(b"not-a-fernet-token")
    with pytest.raises(InvalidToken):
        Fernet(_DEFAULT_KEY).decrypt(b"not-a-fernet-token")
