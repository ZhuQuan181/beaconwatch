"""Domain models shared by the server (HTTP, DB, MQTT)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
    position: Position
    drone_timestamp: datetime | None
    received_at: datetime
    self_id: str = ""
    operator_location: Position | None = None


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
