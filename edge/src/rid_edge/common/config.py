"""Edge station configuration loaded from env / .env file.

We use pydantic-settings so the same config object can be wired into
CLI args, systemd EnvironmentFile, and docker-compose env: section
without code changes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. All values overridable via env vars."""

    model_config = SettingsConfigDict(
        env_prefix="RID_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Station identity ----
    station_id: str = Field(
        default="station-01",
        min_length=1,
        max_length=64,
        description="Unique edge station ID (used as MQTT topic suffix)",
    )
    station_name: str | None = Field(default=None, max_length=128)
    station_lat: float = Field(default=0.0, ge=-90, le=90)
    station_lon: float = Field(default=0.0, ge=-180, le=180)
    station_alt_m: float = Field(default=0.0, ge=-1000, le=100000)
    firmware_version: str = Field(default="0.9.0", max_length=32)

    # ---- Wi-Fi receiver ----
    wifi_enabled: bool = Field(default=True)
    wifi_interface: str = Field(
        default="wlan1mon",
        description="Monitor-mode interface name (e.g. wlan1mon, mon0)",
    )
    wifi_channel_hop: bool = Field(
        default=True,
        description="Hop between 2.4G channels if no fixed channel",
    )
    wifi_fixed_channel: int | None = Field(
        default=None,
        ge=1,
        le=196,
        description="Lock to a specific channel (e.g. 6 or 36). Disables hopping.",
    )
    wifi_rssi_min_dbm: int = Field(
        default=-90,
        ge=-150,
        le=0,
        description="Drop frames weaker than this (noise floor)",
    )

    # ---- BLE receiver ----
    ble_enabled: bool = Field(default=True)
    ble_scan_interval_s: float = Field(default=1.0, gt=0, le=10)
    ble_scan_window_s: float = Field(default=0.3, gt=0, le=10)
    ble_rssi_min_dbm: int = Field(default=-90, ge=-150, le=0)

    # ---- Dedup ----
    dedup_window_s: float = Field(
        default=1.0,
        gt=0,
        description="Suppress same drone emission within this window",
    )
    dedup_max_keys: int = Field(default=4096, ge=16)
    max_obs_per_sec: int = Field(
        default=50,
        ge=1,
        le=1000,
        description="Per-station outgoing observation rate cap",
    )

    # ---- Receiver supervision ----
    receiver_backoff_base_s: float = Field(
        default=1.0,
        gt=0,
        le=60,
        description="Initial restart delay after a receiver crash",
    )
    receiver_backoff_max_s: float = Field(
        default=30.0,
        gt=0,
        le=600,
        description="Maximum receiver restart delay",
    )

    # ---- Local buffer (offline safe) ----
    buffer_db_path: Path = Field(
        default=Path("/var/lib/rid-edge/buffer.sqlite"),
        description="SQLite path for offline queue. Parent dir must exist.",
    )
    buffer_max_bytes: int = Field(
        default=100 * 1024 * 1024,
        description="Hard cap; oldest rows evicted when exceeded",
    )

    # ---- MQTT ----
    mqtt_host: str = Field(default="localhost")
    mqtt_port: int = Field(default=8883, ge=1, le=65535)
    mqtt_use_tls: bool = Field(default=True)
    mqtt_ca_cert: Path | None = Field(default=None)
    mqtt_client_cert: Path | None = Field(default=None)
    mqtt_client_key: Path | None = Field(default=None)
    mqtt_username: str | None = Field(default=None)
    mqtt_password: str | None = Field(default=None)
    mqtt_client_id_prefix: str = Field(default="rid-edge")
    mqtt_keepalive_s: int = Field(default=60, ge=10, le=3600)
    mqtt_qos: Literal[0, 1, 2] = Field(default=1)
    mqtt_ack_timeout_s: float = Field(
        default=10.0,
        gt=0,
        le=120,
        description="Wait this long for a QoS 1/2 PUBACK before re-buffering",
    )
    mqtt_topic_prefix: str = Field(default="rid/v1")

    # ---- Station heartbeat ----
    info_interval_s: float = Field(default=60.0, gt=0)

    # ---- Logging ----
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    log_json: bool = Field(default=False, description="JSON logs for journald ingestion")

    # ---- Derived ----
    @property
    def mqtt_client_id(self) -> str:
        return f"{self.mqtt_client_id_prefix}-{self.station_id}"

    @property
    def topic_info(self) -> str:
        return f"{self.mqtt_topic_prefix}/{self.station_id}/info"

    @property
    def topic_obs(self) -> str:
        return f"{self.mqtt_topic_prefix}/{self.station_id}/obs"


_settings: Settings | None = None


def get_settings() -> Settings:
    """Module-level singleton (lazy-initialized)."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reload_settings(**overrides) -> Settings:
    """Force-reload (used in tests)."""
    global _settings
    _settings = Settings(**overrides)
    return _settings
