"""Application settings, loaded once at import time."""
from __future__ import annotations

import os
import re
import socket
from functools import lru_cache
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_instance_id() -> str:
    host = re.sub(r"[^A-Za-z0-9._-]", "-", socket.gethostname())
    host = host.strip("-")[:48] or "server"
    return f"{host}-{os.getpid()}"[:64]


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
    # to require `Authorization: Bearer <token>` on data routes.  The
    # legacy token is an administrator credential; use a key file for
    # named identities and least-privilege roles.
    api_token: SecretStr | None = None
    # JSON credential file with per-operator SHA-256 API-key digests.
    # When configured, callers must present one of those keys and the
    # API enforces viewer/operator/admin capabilities.
    auth_users_file: str = ""
    auth_reload_interval_s: float = Field(default=5.0, ge=1.0, le=300.0)
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
    # Optional MQTT shared-subscription group. Set the same non-empty
    # value on every API replica so each published message is consumed
    # by exactly one replica instead of every replica. Leave empty for
    # a single-process deployment.
    mqtt_shared_group: str = ""
    mqtt_qos: int = Field(default=1, ge=0, le=2)
    # Unique per server process/replica.  Appended to the MQTT client id
    # so scaling the API does not make replicas kick each other off the
    # broker.  Defaults to the container/host name.
    instance_id: str = Field(
        default_factory=_default_instance_id,
        max_length=64,
    )

    # ── Live WebSocket bus ──────────────────────────────────────────
    # memory is correct for one API process.  redis enables cross-replica
    # live fan-out while leaving ingest idempotent.
    bus_backend: str = "memory"
    redis_url: str = "redis://localhost:6379/0"
    redis_channel: str = "beaconwatch:events"
    redis_required: bool = False
    redis_connect_timeout_s: float = Field(default=3.0, ge=0.1, le=30.0)
    redis_publish_timeout_s: float = Field(default=1.0, ge=0.1, le=30.0)
    # Upper bound for one idle Pub/Sub read. This is not a socket
    # timeout: the connection itself stays open, while the listener
    # wakes periodically to notice stop/disconnect requests.
    redis_poll_timeout_s: float = Field(default=1.0, ge=0.05, le=5.0)
    redis_reconnect_max_s: float = Field(default=30.0, ge=1.0, le=300.0)

    # ── Alert rules ─────────────────────────────────────────────────
    # Altitude ceiling in metres.  None disables the rule.
    alert_max_alt_m: float | None = 120.0
    # Whether a drone broadcasting no operator ID raises an alert.
    alert_unidentified: bool = True
    # Re-alert interval for the same (drone, rule, zone).  A drone
    # hovering in a no-fly zone emits 4 fixes/second; without this the
    # panel is unusable within a minute.
    alert_dedup_seconds: float = Field(
        default=120.0,
        ge=0.0,
        le=7 * 24 * 60 * 60,
    )
    # Zero disables destructive cleanup.  Operators must opt in to a
    # retention policy because alert events are an audit trail.
    alert_retention_days: int = Field(default=0, ge=0, le=3650)
    notification_retention_days: int = Field(default=0, ge=0, le=3650)
    retention_interval_s: float = Field(default=3600.0, ge=60.0, le=86400.0)
    retention_batch_size: int = Field(default=500, ge=1, le=5000)

    # ── Alert notification webhook ──────────────────────────────────
    # Empty URL disables notification enqueueing and the worker.  When a
    # URL is set, a signing secret is mandatory and deliveries use the
    # stable ``delivery_id`` as their idempotency key.
    notify_webhook_url: str = ""
    notify_webhook_secret: SecretStr | None = None
    notify_webhook_timeout_s: float = Field(default=10.0, ge=0.5, le=60.0)
    # Direct egress is the safe default: a globally exported proxy can
    # otherwise turn an internal webhook into a proxy-routed request.
    notify_webhook_trust_env: bool = False
    notify_worker_interval_s: float = Field(default=1.0, ge=0.1, le=60.0)
    notify_batch_size: int = Field(default=20, ge=1, le=100)
    notify_concurrency: int = Field(default=4, ge=1, le=32)
    notify_max_attempts: int = Field(default=8, ge=1, le=50)
    notify_base_backoff_s: float = Field(default=2.0, ge=0.1, le=60.0)
    notify_max_backoff_s: float = Field(default=300.0, ge=1.0, le=3600.0)

    # ── Operational ────────────────────────────────────────────────
    log_level: str = "INFO"
    env: str = "dev"

    @field_validator("instance_id", mode="before")
    @classmethod
    def normalize_instance_id(cls, value: object) -> str:
        """Use a process-unique default when Compose passes an empty var."""

        if value is None:
            return _default_instance_id()
        text = str(value).strip()
        return text or _default_instance_id()

    @field_validator("mqtt_shared_group")
    @classmethod
    def validate_shared_group(cls, value: str) -> str:
        group = value.strip()
        if not group:
            return ""
        if len(group) > 64 or not re.fullmatch(r"[A-Za-z0-9._-]+", group):
            raise ValueError(
                "RID_MQTT_SHARED_GROUP must be 1-64 characters from "
                "A-Z, a-z, 0-9, '.', '_' or '-'"
            )
        return group

    @model_validator(mode="after")
    def validate_notification_config(self) -> Settings:
        """Fail fast on a webhook that cannot be safely delivered."""
        if not self.notify_webhook_url:
            return self
        parsed = urlparse(self.notify_webhook_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                "RID_NOTIFY_WEBHOOK_URL must be an absolute http(s) URL"
            )
        if (
            self.notify_webhook_secret is None
            or not self.notify_webhook_secret.get_secret_value()
        ):
            raise ValueError(
                "RID_NOTIFY_WEBHOOK_SECRET is required when "
                "RID_NOTIFY_WEBHOOK_URL is set"
            )
        if self.notify_max_backoff_s < self.notify_base_backoff_s:
            raise ValueError(
                "RID_NOTIFY_MAX_BACKOFF_S must be >= "
                "RID_NOTIFY_BASE_BACKOFF_S"
            )
        return self

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

    @property
    def notifications_enabled(self) -> bool:
        return bool(self.notify_webhook_url)

    @property
    def mqtt_client_identifier(self) -> str:
        base = (self.mqtt_client_id or "rid-server").strip()
        suffix = (self.instance_id or _default_instance_id()).strip()
        value = f"{base}-{suffix}" if suffix else base
        return value[:128]

    @property
    def mqtt_obs_subscription_topic(self) -> str:
        return self._shared_topic(self.mqtt_obs_topic)

    @property
    def mqtt_info_subscription_topic(self) -> str:
        return self._shared_topic(self.mqtt_info_topic)

    def _shared_topic(self, topic: str) -> str:
        if not self.mqtt_shared_group:
            return topic
        return f"$share/{self.mqtt_shared_group}/{topic}"

    @model_validator(mode="after")
    def validate_bus_config(self) -> Settings:
        if self.bus_backend not in {"memory", "redis"}:
            raise ValueError(
                "RID_BUS_BACKEND must be either 'memory' or 'redis'"
            )
        if self.bus_backend == "redis":
            parsed = urlparse(self.redis_url)
            if parsed.scheme not in {"redis", "rediss"} or not parsed.netloc:
                raise ValueError(
                    "RID_REDIS_URL must be an absolute redis:// or rediss:// URL"
                )
            if not self.redis_channel.strip():
                raise ValueError("RID_REDIS_CHANNEL must not be empty")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton — call ``get_settings.cache_clear()`` in tests."""
    return Settings()
