"""Shared RID data models — canonical schema for the whole system.

Flow:
    edge receiver -> parser -> dedup -> MQTT publisher -> server ingest

All edges and the server agree on the wire format defined by RidObservation.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Protocol(str, enum.Enum):
    """Underlying broadcast protocol that produced the observation."""

    ASTM_F3411 = "astm_f3411"
    WIFI_NAN = "wifi_nan"


class Position(BaseModel):
    """WGS-84 geodetic position.

    Altitude is HAE (Height Above Ellipsoid), per ASTM F3411 convention.

    Motion fields live here, not on the observation, because ASTM F3411
    carries them in the Location message — and because the server's
    ``Position`` requires them, so putting them anywhere else makes the
    message unparseable at the far end.  See tests/test_wire_contract.py.
    """

    model_config = ConfigDict(frozen=False)

    lat: float = Field(ge=-90.0, le=90.0, description="Latitude in degrees")
    lon: float = Field(ge=-180.0, le=180.0, description="Longitude in degrees")
    alt_m: float = Field(
        ge=-1000.0,
        le=100000.0,
        description="Altitude in meters (WGS-84 HAE)",
    )
    accuracy_h_m: float = Field(
        default=10.0,
        ge=0.0,
        description="Horizontal accuracy (1-sigma, meters)",
    )
    accuracy_v_m: float = Field(
        default=15.0,
        ge=0.0,
        description="Vertical accuracy (1-sigma, meters)",
    )
    speed_mps: float = Field(
        default=0.0,
        ge=0.0,
        le=500.0,
        description="Ground speed in m/s (ASTM Speed field)",
    )
    heading_deg: float = Field(
        default=0.0,
        ge=0.0,
        le=360.0,
        description="Direction of travel in degrees true north",
    )

    @field_validator("lat", "lon")
    @classmethod
    def _finite(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("must be finite")
        return v


class OperatorLocation(BaseModel):
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    alt_m: float = Field(default=0.0, ge=-1000.0, le=100000.0)


class RidObservation(BaseModel):
    """A single RID observation produced by an edge receiver.

    This is the canonical data structure that flows through the system.
    Wire format: JSON serialization via to_mqtt_payload().
    """

    model_config = ConfigDict(use_enum_values=False)

    # ---- Identity ----
    observation_id: str = Field(
        min_length=1,
        max_length=64,
        description="ULID/UUID, unique per emit (idempotency key at the server)",
    )
    drone_id: str = Field(
        min_length=1,
        max_length=64,
        description="UAS ID from the ASTM Basic ID message (or peer MAC for Wi-Fi NaN)",
    )
    operator_id: str | None = Field(default=None, max_length=64)

    # ---- Source ----
    protocol: Protocol
    station_id: str = Field(
        min_length=1,
        max_length=64,
        description="Edge station (industrial PC) ID",
    )
    rssi_dbm: int = Field(
        ge=-150,
        le=0,
        description="Received signal strength at the edge receiver",
    )

    # ---- Geometry ----
    # Speed and heading are NOT duplicated here — they live in Position,
    # matching both ASTM F3411 (they're Location fields) and the server
    # schema.  See tests/test_wire_contract.py.
    position: Position

    # ---- Provenance ----
    drone_timestamp: datetime = Field(
        description="When the drone claims it was at this position (per ASTM timestamp)",
    )
    received_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the edge station received the broadcast (UTC)",
    )
    operator_location: OperatorLocation | None = None
    self_id: str | None = Field(
        default=None,
        max_length=256,
        description="Free-text self-ID broadcast by the drone (ASTM Self-ID message)",
    )

    # ---- Wire format helpers ----
    def to_mqtt_payload(self) -> str:
        """Serialize to JSON for MQTT publish."""
        return self.model_dump_json()

    @classmethod
    def from_mqtt_payload(cls, data: str | bytes) -> "RidObservation":
        """Parse JSON payload from MQTT subscription."""
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        return cls.model_validate_json(data)


class StationInfo(BaseModel):
    """Self-description of the edge station (industrial PC).

    Published to MQTT topic `rid/v1/{station_id}/info` periodically so
    the server knows each station's location and identity.
    """

    station_id: str = Field(min_length=1, max_length=64)
    location: Position
    name: str | None = Field(default=None, max_length=128)
    firmware_version: str = Field(default="0.1.0", max_length=32)
    capabilities: list[str] = Field(
        default_factory=lambda: ["ble", "wifi_nan"],
        description="Receiver capabilities: ble, wifi_nan",
    )
