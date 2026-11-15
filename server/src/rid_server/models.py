"""Domain models shared by the server (HTTP, DB, MQTT)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Position(BaseModel):
    """WGS-84 lat/lon/alt with accuracy.

    Matches the edge ``Position`` exactly so MQTT payloads can be
    ``model_validate``'d directly with no field-by-field marshaling.
    """

    model_config = ConfigDict(extra="forbid")

    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    alt_m: float = Field(ge=-1000.0, le=10000.0)
    accuracy_h_m: float = Field(ge=0.0, le=1000.0)
    accuracy_v_m: float = Field(ge=0.0, le=1000.0)
    speed_mps: float = Field(ge=0.0, le=500.0)
    heading_deg: float = Field(ge=0.0, le=360.0)


class OperatorLocation(BaseModel):
    """Operator/pilot location carried by the ODID System message.

    The System message contains only the operator's geodetic position;
    speed, heading and accuracy belong to the aircraft, not the pilot.
    Reusing :class:`Position` here rejected every valid System message
    because it demanded fields that do not exist on the wire.
    """

    # Older rows stored a full aircraft Position under this key.  Ignore
    # its extra motion/accuracy fields so upgrades can still replay data.
    model_config = ConfigDict(extra="ignore")

    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    alt_m: float = Field(ge=-1000.0, le=10000.0)


class DroneObservation(BaseModel):
    """Server-side view of a single observation.

    Note: protocol/transport fields differ slightly from the edge
    model because the server adds a primary key and station ref.
    """

    model_config = ConfigDict(extra="ignore")

    observation_id: str
    drone_id: str
    operator_id: str | None = None
    station_id: str
    protocol: Literal[
        "wifi_nan", "ble", "wifi_beacon", "astm_f3411", "unknown"
    ]
    rssi_dbm: int | None = Field(
        default=None,
        ge=-150,
        le=0,
        description="Signal strength at the receiving station (dBm)",
    )
    position: Position
    drone_timestamp: datetime | None
    received_at: datetime
    self_id: str | None = Field(
        default="",
        description="Free-text self-ID; the edge sends null when the drone "
        "broadcasts no Self-ID message, so this must accept None.",
    )
    operator_location: OperatorLocation | None = None


class ObservationPage(BaseModel):
    """Keyset-paginated history page."""

    items: list[DroneObservation]
    next_cursor: str | None = None


class Station(BaseModel):
    """A receiving station (edge node)."""

    model_config = ConfigDict(extra="ignore")

    station_id: str
    name: str
    lat: float
    lon: float
    alt_m: float
    online: bool = True
    last_seen: datetime | None = None
    firmware_version: str = ""
    coverage_radius_m: float = 3000.0


class StationInfo(BaseModel):
    """Edge station heartbeat published to ``rid/v1/{station_id}/info``."""

    model_config = ConfigDict(extra="ignore")

    station_id: str = Field(min_length=1, max_length=64)
    location: Position
    name: str | None = Field(default=None, max_length=128)
    firmware_version: str = Field(default="0.5.0", max_length=32)
    capabilities: list[str] = Field(default_factory=lambda: ["ble", "wifi_nan"])


class GeofenceZoneOut(BaseModel):
    """A no-fly volume, as the map and the rule engine both see it."""

    zone_id: str
    name: str
    polygon: list[list[float]]
    min_alt_m: float | None = None
    max_alt_m: float | None = None
    severity: str = "warn"


class AlertOut(BaseModel):
    """An alert event as returned by the API / pushed over WebSocket."""

    alert_id: str
    rule: str
    severity: str
    drone_id: str
    message: str
    detail: dict = Field(default_factory=dict)
    at: str = ""
    first_seen_at: str | None = None
    trigger_count: int = 1
    status: Literal["open", "acknowledged", "resolved"] = "open"
    updated_at: str | None = None
    acknowledged_at: str | None = None
    acknowledged_by: str | None = None
    acknowledgement_note: str | None = None
    resolved_at: str | None = None
    resolved_by: str | None = None
    resolution_note: str | None = None
    reopen_count: int = 0


class AlertAckIn(BaseModel):
    """Operator acknowledgement for one alert.

    The first acknowledgement wins.  Repeating the request is idempotent
    so a retried HTTP call cannot overwrite the original audit trail.
    """

    model_config = ConfigDict(extra="forbid")

    acknowledged_by: str = Field(min_length=1, max_length=128)
    note: str = Field(default="", max_length=1000)

    @field_validator("acknowledged_by")
    @classmethod
    def acknowledged_by_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("acknowledged_by must not be blank")
        return value


class AlertResolveIn(BaseModel):
    """Close an alert and record who determined it was handled."""

    model_config = ConfigDict(extra="forbid")

    resolved_by: str = Field(min_length=1, max_length=128)
    note: str = Field(default="", max_length=1000)

    @field_validator("resolved_by")
    @classmethod
    def resolved_by_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("resolved_by must not be blank")
        return value


class AlertReopenIn(BaseModel):
    """Reopen a resolved alert as a new active handling cycle."""

    model_config = ConfigDict(extra="forbid")

    reopened_by: str = Field(min_length=1, max_length=128)
    note: str = Field(default="", max_length=1000)

    @field_validator("reopened_by")
    @classmethod
    def reopened_by_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reopened_by must not be blank")
        return value


class AlertEventOut(BaseModel):
    """One immutable alert lifecycle event."""

    event_id: str
    alert_id: str
    action: Literal[
        "triggered",
        "acknowledged",
        "resolved",
        "reopened",
    ]
    actor: str | None = None
    note: str | None = None
    at: str
    detail: dict = Field(default_factory=dict)


class DroneSummary(BaseModel):
    """Aggregated per-drone view (latest position + first/last seen)."""

    drone_id: str
    uas_id: str
    ua_type: str
    first_seen: datetime
    last_seen: datetime
    last_position: Position
    last_station_id: str
    observation_count: int
