"""Settings secret-file and DSN tests."""
from __future__ import annotations

import pytest
from pydantic import ValidationError
from rid_server.config import Settings


def test_secret_files_override_inline_defaults(tmp_path):
    files = {
        "db": "database-secret\n",
        "api": "api-secret\n",
        "mqtt": "mqtt-secret\n",
        "redis": "rediss://:redis-secret@redis.example:6380/0\n",
        "webhook": "webhook-secret\n",
    }
    paths = {}
    for name, value in files.items():
        path = tmp_path / f"{name}.secret"
        path.write_text(value, encoding="utf-8")
        paths[name] = path

    settings = Settings(
        db_password_file=str(paths["db"]),
        api_token_file=str(paths["api"]),
        mqtt_password_file=str(paths["mqtt"]),
        redis_url_file=str(paths["redis"]),
        notify_webhook_url="https://hooks.example.test/alerts",
        notify_webhook_secret_file=str(paths["webhook"]),
    )

    assert settings.db_password == "database-secret"
    assert settings.api_token is not None
    assert settings.api_token.get_secret_value() == "api-secret"
    assert settings.mqtt_password == "mqtt-secret"
    assert settings.redis_url == files["redis"].strip()
    assert settings.notify_webhook_secret is not None
    assert (
        settings.notify_webhook_secret.get_secret_value()
        == "webhook-secret"
    )

    representation = repr(settings)
    for secret in (
        "database-secret",
        "api-secret",
        "mqtt-secret",
        "redis-secret",
        "webhook-secret",
    ):
        assert secret not in representation


def test_missing_secret_file_fails_fast(tmp_path):
    with pytest.raises(ValidationError, match="RID_DB_PASSWORD_FILE"):
        Settings(db_password_file=str(tmp_path / "missing"))


def test_empty_secret_file_is_rejected(tmp_path):
    path = tmp_path / "empty"
    path.write_text("\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="must not be empty"):
        Settings(db_password_file=str(path))


def test_oversized_secret_file_is_rejected(tmp_path):
    path = tmp_path / "large"
    path.write_text("x" * (64 * 1024 + 1), encoding="utf-8")
    with pytest.raises(ValidationError, match="larger than 64 KiB"):
        Settings(db_password_file=str(path))


def test_database_url_percent_encodes_credentials_and_database():
    settings = Settings(
        db_user="rid user",
        db_password="p@ss:/#?",
        db_name="rid db",
    )

    assert settings.database_url == (
        "postgresql+asyncpg://rid%20user:"
        "p%40ss%3A%2F%23%3F@localhost:5432/rid%20db"
    )
