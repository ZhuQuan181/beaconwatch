"""Tests for shared RID data models — TDD: written before implementation."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from rid_edge.models import (
    Position,
    Protocol,
    RidObservation,
    StationInfo,
)


def test_position_validates_lat_range():
    with pytest.raises(ValidationError):
        Position(lat=91, lon=0, alt_m=100)


def test_position_validates_lon_range():
    with pytest.raises(ValidationError):
        Position(lat=0, lon=181, alt_m=100)


def test_position_rejects_nan():
    with pytest.raises(ValidationError):
        Position(lat=float("nan"), lon=0, alt_m=100)


def test_position_accepts_valid_values():
    p = Position(lat=37.7749, lon=-122.4194, alt_m=120.0)
    assert p.lat == 37.7749
    assert p.alt_m == 120.0
    assert p.accuracy_h_m == 10.0  # default


def test_observation_round_trip_json():
    obs = RidObservation(
        observation_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        drone_id="drone-001",
        operator_id="op-42",
        protocol=Protocol.ASTM_F3411,
        station_id="station-01",
        rssi_dbm=-65,
        position=Position(
            lat=37.7749,
            lon=-122.4194,
            alt_m=120.0,
            speed_mps=15.5,
            heading_deg=90.0,
        ),
        drone_timestamp=datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc),
        self_id="survey mission",
    )
    payload = obs.to_mqtt_payload()
    restored = RidObservation.from_mqtt_payload(payload)
    assert restored == obs


def test_observation_default_received_at_utc():
    obs = RidObservation(
        observation_id="x",
        drone_id="d1",
        protocol=Protocol.WIFI_NAN,
        station_id="s1",
        rssi_dbm=-50,
        position=Position(lat=0, lon=0, alt_m=0),
        drone_timestamp=datetime.now(timezone.utc),
    )
    assert obs.received_at.tzinfo is not None


def test_observation_serializes_enum_as_string():
    obs = RidObservation(
        observation_id="x",
        drone_id="d1",
        protocol=Protocol.ASTM_F3411,
        station_id="s1",
        rssi_dbm=-50,
        position=Position(lat=0, lon=0, alt_m=0),
        drone_timestamp=datetime.now(timezone.utc),
    )
    payload = obs.to_mqtt_payload()
    assert '"protocol":"astm_f3411"' in payload


def test_observation_rejects_invalid_rssi():
    with pytest.raises(ValidationError):
        RidObservation(
            observation_id="x",
            drone_id="d1",
            protocol=Protocol.ASTM_F3411,
            station_id="s1",
            rssi_dbm=10,  # invalid: must be <= 0
            position=Position(lat=0, lon=0, alt_m=0),
            drone_timestamp=datetime.now(timezone.utc),
        )


def test_observation_accepts_bytes_payload():
    obs = RidObservation(
        observation_id="01ABC",
        drone_id="d2",
        protocol=Protocol.WIFI_NAN,
        station_id="s2",
        rssi_dbm=-80,
        position=Position(lat=1, lon=2, alt_m=10),
        drone_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    raw = obs.to_mqtt_payload().encode("utf-8")
    restored = RidObservation.from_mqtt_payload(raw)
    assert restored.drone_id == "d2"


def test_station_info_basic():
    s = StationInfo(
        station_id="edge-01",
        location=Position(lat=37.7, lon=-122.4, alt_m=10),
        name="Downtown Pier 1",
    )
    assert s.station_id == "edge-01"
    assert s.firmware_version == "1.0.0"
    assert "ble" in s.capabilities
