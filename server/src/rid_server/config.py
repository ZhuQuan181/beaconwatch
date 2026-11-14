"""Application settings, loaded once at import time."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven application config.

    All variables are prefixed with ``RID_`` and lowercased.  See
    ``.env.example`` for the full list.
    """

    model_config = SettingsConfigDict(
        env_prefix="RID_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── HTTP server ─────────────────────────────────────────────────
    http_host: str = "0.0.0.0"
    http_port: int = 8000
    # Empty means "open" for local/demo deployments.  Set RID_API_TOKEN
    # to require `Authorization: Bearer <token>` on data routes.
    api_token: SecretStr | None = None
    # Comma-separated origins; empty keeps the same-origin default.
    cors_origins: str = ""
    ws_ping_interval_s: float = Field(default=20.0, ge=5.0, le=120.0)
    unidentified_lookback_s: int = Field(
        default=7 * 24 * 60 * 60,
        ge=60,
        le=30 * 24 * 60 * 60,
        description="Default recent window for /drones/unidentified",
    )

    # ── PostgreSQL / TimescaleDB ────────────────────────────────────
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "rid"
    db_user: str = "rid"
    db_password: str = "rid_dev_password"
    db_pool_min: int = 2
    db_pool_max: int = 10

    # ── MQTT broker ─────────────────────────────────────────────────
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_tls: bool = False
    mqtt_ca_cert: str = ""
    mqtt_client_cert: str = ""
    mqtt_client_key: str = ""
    mqtt_client_id: str = "rid-server"
    mqtt_obs_topic: str = "rid/v1/+/obs"
    mqtt_info_topic: str = "rid/v1/+/info"
    mqtt_qos: int = Field(default=1, ge=0, le=2)

    # ── Alert rules ─────────────────────────────────────────────────
    # Altitude ceiling in metres.  None disables the rule.
    alert_max_alt_m: float | None = 120.0
    # Whether a drone broadcasting no operator ID raises an alert.
    alert_unidentified: bool = True
    # Re-alert interval for the same (drone, rule, zone).  A drone
    # hovering in a no-fly zone emits 4 fixes/second; without this the
    # panel is unusable within a minute.
    alert_dedup_seconds: float = 120.0

    # ── Operational ────────────────────────────────────────────────
    log_level: str = "INFO"
    env: str = "dev"

    @property
    def database_url(self) -> str:
        """Async DSN — uses asyncpg driver for SQLAlchemy 2.0."""
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton — call ``get_settings.cache_clear()`` in tests."""
    return Settings()
