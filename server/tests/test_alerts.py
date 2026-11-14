"""Alert rule tests — pure functions, no broker or database."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from rid_server.alerts import AlertEngine, Zone, point_in_polygon

# A 1 km-ish square around Nanshan, Shenzhen — the fixture zone.
SQUARE = [
    [113.9300, 22.5350],
    [113.9500, 22.5350],
    [113.9500, 22.5500],
    [113.9300, 22.5500],
]


@dataclass
class FakePos:
    lat: float
    lon: float
    alt_m: float


@dataclass
class FakeObs:
    drone_id: str
    operator_id: str | None
    position: FakePos
    received_at: datetime = datetime(2026, 11, 14, 10, 0, 0, tzinfo=timezone.utc)


def obs(lat=22.5420, lon=113.9400, alt=100.0, op="OP-1", drone="D-1"):
    return FakeObs(
        drone_id=drone,
        operator_id=op,
        position=FakePos(lat=lat, lon=lon, alt_m=alt),
    )


# ── Point in polygon ─────────────────────────────────────────────────
def test_point_inside_square():
    assert point_in_polygon(113.9400, 22.5420, SQUARE) is True


def test_point_outside_square():
    assert point_in_polygon(113.9000, 22.5420, SQUARE) is False
    assert point_in_polygon(113.9400, 22.6000, SQUARE) is False


def test_degenerate_polygon_is_never_inside():
    """Fewer than 3 points can't contain anything — must not raise."""
    assert point_in_polygon(113.94, 22.54, []) is False
    assert point_in_polygon(113.94, 22.54, [[113.94, 22.54]]) is False
    assert point_in_polygon(113.94, 22.54, [[113.94, 22.54], [113.95, 22.55]]) is False


def test_non_convex_polygon():
    """An L-shape: the notch must be outside.

    Ray casting handles concavity; a naive bounding-box check would
    report the notch as inside, and airport exclusion zones are rarely
    convex.
    """
    l_shape = [
        [0.0, 0.0],
        [2.0, 0.0],
        [2.0, 1.0],
        [1.0, 1.0],
        [1.0, 2.0],
        [0.0, 2.0],
    ]
    assert point_in_polygon(0.5, 0.5, l_shape) is True   # in the corner
    assert point_in_polygon(1.5, 0.5, l_shape) is True   # in the arm
    assert point_in_polygon(1.5, 1.5, l_shape) is False  # in the notch


# ── Geofence rule ────────────────────────────────────────────────────
def test_geofence_breach_fires():
    eng = AlertEngine(max_alt_m=None, unidentified=False)
    zone = Zone(zone_id="nfz-1", name="机场净空区", polygon=SQUARE)
    alerts = eng.evaluate(obs(), [zone])
    assert len(alerts) == 1
    a = alerts[0]
    assert a.rule == "geofence"
    assert a.severity == "warn"
    assert "机场净空区" in a.message
    assert a.detail["zone_id"] == "nfz-1"


def test_geofence_respects_altitude_band():
    """A zone bounded to 0-50 m must not fire at 200 m."""
    eng = AlertEngine(max_alt_m=None, unidentified=False)
    zone = Zone(zone_id="low", name="低空区", polygon=SQUARE,
                min_alt_m=0, max_alt_m=50)
    assert eng.evaluate(obs(alt=30), [zone]) != []
    assert eng.evaluate(obs(alt=200, drone="D-2"), [zone]) == []


def test_geofence_silent_outside():
    eng = AlertEngine(max_alt_m=None, unidentified=False)
    zone = Zone(zone_id="nfz-1", name="Z", polygon=SQUARE)
    assert eng.evaluate(obs(lat=22.6000, lon=113.9400), [zone]) == []


# ── Altitude rule ────────────────────────────────────────────────────
def test_altitude_limit_fires_above_limit():
    eng = AlertEngine(max_alt_m=120.0, unidentified=False)
    alerts = eng.evaluate(obs(alt=150.0))
    assert [a.rule for a in alerts] == ["altitude"]
    assert alerts[0].detail["limit_m"] == 120.0


def test_altitude_limit_silent_below():
    eng = AlertEngine(max_alt_m=120.0, unidentified=False)
    assert eng.evaluate(obs(alt=119.9)) == []


def test_altitude_limit_exact_boundary_is_ok():
    """At the limit is compliant; only exceeding it is a breach."""
    eng = AlertEngine(max_alt_m=120.0, unidentified=False)
    assert eng.evaluate(obs(alt=120.0)) == []


# ── Unidentified rule ────────────────────────────────────────────────
def test_unidentified_fires_without_operator_id():
    eng = AlertEngine(max_alt_m=None)
    alerts = eng.evaluate(obs(op=None))
    assert [a.rule for a in alerts] == ["unidentified"]
    assert alerts[0].severity == "critical"


def test_identified_drone_is_silent():
    eng = AlertEngine(max_alt_m=None)
    assert eng.evaluate(obs(op="OP-9")) == []


# ── Deduplication ────────────────────────────────────────────────────
def test_geofence_dedups_within_window():
    """A hovering drone at 4 Hz must not emit 4 alerts/second."""
    eng = AlertEngine(max_alt_m=None, unidentified=False, dedup_seconds=120)
    zone = Zone(zone_id="nfz-1", name="Z", polygon=SQUARE)
    assert len(eng.evaluate(obs(), [zone])) == 1
    assert eng.evaluate(obs(), [zone]) == []  # same drone, same zone, no time passed


def test_geofence_repeats_after_window():
    now = [0.0]
    eng = AlertEngine(
        max_alt_m=None,
        unidentified=False,
        dedup_seconds=60,
        clock=lambda: now[0],
    )
    zone = Zone(zone_id="nfz-1", name="Z", polygon=SQUARE)
    assert len(eng.evaluate(obs(), [zone])) == 1
    now[0] = 61.0
    assert len(eng.evaluate(obs(), [zone])) == 1


def test_unidentified_fires_once_per_session():
    """Unidentified is a property of the drone, not of the moment."""
    now = [0.0]
    eng = AlertEngine(max_alt_m=None, dedup_seconds=1, clock=lambda: now[0])
    assert len(eng.evaluate(obs(op=None))) == 1
    now[0] = 10_000.0  # long past any window
    assert eng.evaluate(obs(op=None)) == []


def test_different_drones_alert_independently():
    eng = AlertEngine(max_alt_m=None, dedup_seconds=60)
    assert len(eng.evaluate(obs(op=None, drone="A"))) == 1
    assert len(eng.evaluate(obs(op=None, drone="B"))) == 1


def test_different_zones_alert_independently():
    eng = AlertEngine(max_alt_m=None, unidentified=False, dedup_seconds=60)
    z1 = Zone(zone_id="z1", name="A", polygon=SQUARE)
    z2 = Zone(zone_id="z2", name="B", polygon=SQUARE, severity="critical")
    alerts = eng.evaluate(obs(), [z1, z2])
    assert len(alerts) == 2
    assert {a.severity for a in alerts} == {"warn", "critical"}


# ── Multiple rules at once ───────────────────────────────────────────
def test_one_observation_can_trip_several_rules():
    """Inside a zone, over the ceiling, no operator ID — three alerts."""
    eng = AlertEngine(max_alt_m=120.0, unidentified=True)
    zone = Zone(zone_id="nfz-1", name="Z", polygon=SQUARE)
    rules = {a.rule for a in eng.evaluate(obs(alt=200.0, op=None), [zone])}
    assert rules == {"geofence", "altitude", "unidentified"}


def test_engine_survives_missing_shifted_geometry():
    """Malformed polygon from the DB must not kill ingest."""
    eng = AlertEngine(max_alt_m=None, unidentified=False)
    bad = Zone(zone_id="bad", name="bad", polygon=[[113.94], [113.95]])
    assert eng.evaluate(obs(), [bad]) == []


@pytest.mark.parametrize("alt,expected", [(0, 0), (119, 0), (121, 1), (500, 1)])
def test_altitude_threshold_table(alt, expected):
    eng = AlertEngine(max_alt_m=120.0, unidentified=False)
    assert len(eng.evaluate(obs(alt=alt))) == expected


# ── Wiring into ingest ───────────────────────────────────────────────
async def test_ingest_fires_alert_after_persisting(db_engine, _sqlite_settings, monkeypatch):
    """An alert must correspond to a row that's actually stored.

    Rules run after the commit, so anything an operator sees in the alert
    panel can be traced back to an observation in the database.
    """
    from rid_server.mqtt_subscriber import MqttSubscriber
    from rid_server.repository import upsert_station, list_observations
    from rid_server.models import Station as StationModel
    from sqlalchemy.ext.asyncio import async_sessionmaker
    import json

    # Seed a station so the FK is satisfied.
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        await upsert_station(
            s,
            StationModel(
                station_id="st-1", name="X", lat=0.0, lon=0.0, alt_m=0.0,
                firmware_version="", last_seen=None, online=True,
            ),
        )

    collected = []
    sub = MqttSubscriber(_sqlite_settings)
    sub.alerts = AlertEngine(max_alt_m=120.0, unidentified=True)

    async def _collect(alert):
        collected.append(alert)

    sub.on_alert = _collect

    payload = {
        "observation_id": "al-1",
        "drone_id": "ghost-1",
        "operator_id": None,          # unidentified → critical
        "station_id": "st-1",
        "protocol": "astm_f3411",
        "rssi_dbm": -70,
        "position": {
            "lat": 22.5420, "lon": 113.9400, "alt_m": 500.0,  # over the ceiling
            "accuracy_h_m": 5.0, "accuracy_v_m": 8.0,
            "speed_mps": 10.0, "heading_deg": 90.0,
        },
        "drone_timestamp": "2026-11-14T10:00:00Z",
        "received_at": "2026-11-14T10:00:01Z",
        "self_id": None,
        "operator_location": None,
    }
    await sub.handle_message("rid/v1/st-1/obs", json.dumps(payload).encode())

    async with factory() as s:
        rows = await list_observations(s, limit=10)
    assert len(rows) == 1, "observation must be persisted before rules run"

    rules = {a.rule for a in collected}
    assert "unidentified" in rules
    assert "altitude" in rules
