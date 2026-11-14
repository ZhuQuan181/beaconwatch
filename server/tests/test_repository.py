"""Repository layer tests — exercises SQLAlchemy ORM directly."""
from __future__ import annotations

from conftest import make_obs
from rid_server.db import Drone
from rid_server.models import DroneObservation, Position
from rid_server.repository import (
    list_drones,
    list_observations,
    list_stations,
    touch_station,
    upsert_station,
    write_observation,
)
from sqlalchemy.ext.asyncio import AsyncSession


def _obs_model(**kwargs) -> DroneObservation:
    raw = make_obs(**kwargs)
    raw["received_at"] = raw["received_at"]  # Pydantic parses ISO strings
    raw["position"] = Position(**raw["position"])
    raw["drone_timestamp"] = raw["drone_timestamp"]
    return DroneObservation.model_validate(raw)


async def test_upsert_station_inserts_then_updates(session: AsyncSession):
    from rid_server.models import Station as StationModel
    s = StationModel(
        station_id="S-1", name="Pier 1", lat=37.81, lon=-122.41,
        alt_m=10.0, firmware_version="1.0.0", last_seen=None, online=True,
    )
    await upsert_station(session, s)
    s.name = "Pier 1 (updated)"
    s.lat = 37.82
    await upsert_station(session, s)
    rows = await list_stations(session)
    assert len(rows) == 1
    assert rows[0].station_id == "S-1"
    assert rows[0].name == "Pier 1 (updated)"
    assert rows[0].lat == 37.82


async def test_touch_station_updates_last_seen(session: AsyncSession):
    from rid_server.models import Station as StationModel
    s = StationModel(
        station_id="S-2", name="Pier 2", lat=0, lon=0,
        alt_m=0, firmware_version="", last_seen=None, online=True,
    )
    await upsert_station(session, s)
    await touch_station(session, "S-2", online=True)
    rows = await list_stations(session)
    assert rows[0].last_seen is not None
    assert rows[0].online is True


async def test_write_observation_inserts_row_and_upserts_drone(
    session: AsyncSession,
):
    from rid_server.models import Station as StationModel
    await upsert_station(
        session,
        StationModel(
            station_id="station-1", name="S1", lat=0, lon=0,
            alt_m=0, firmware_version="", last_seen=None, online=True,
        ),
    )

    obs = _obs_model(drone_id="D-A", station_id="station-1", lat=10.0, lon=20.0)
    await write_observation(session, obs)

    # Drone row should exist with last_seen set and count=1
    drone = await session.get(Drone, "D-A")
    assert drone is not None
    assert drone.observation_count == 1
    assert drone.last_position_lat == 10.0
    assert drone.last_position_lon == 20.0


async def test_write_observation_idempotent_on_observation_id(
    session: AsyncSession,
):
    from rid_server.models import Station as StationModel
    await upsert_station(
        session,
        StationModel(
            station_id="station-1", name="S1", lat=0, lon=0,
            alt_m=0, firmware_version="", last_seen=None, online=True,
        ),
    )
    obs = _obs_model(drone_id="D-B", station_id="station-1")
    await write_observation(session, obs)
    # Second insert with same observation_id → no-op
    await write_observation(session, obs)

    drone = await session.get(Drone, "D-B")
    # Drone count should still be 1 (UPSERT fires once, then ON CONFLICT
    # DO NOTHING on observations table prevents 2nd increment)
    assert drone is not None
    assert drone.observation_count == 1


async def test_write_observation_monotonic_count(session: AsyncSession):
    from rid_server.models import Station as StationModel
    await upsert_station(
        session,
        StationModel(
            station_id="station-1", name="S1", lat=0, lon=0,
            alt_m=0, firmware_version="", last_seen=None, online=True,
        ),
    )
    base = "drone-C"
    for i in range(3):
        obs = _obs_model(
            drone_id=base,
            station_id="station-1",
        )
        # Distinct observation_ids to avoid idempotent skip
        obs.observation_id = f"obs-{base}-{i}"
        await write_observation(session, obs)
    drone = await session.get(Drone, base)
    assert drone is not None
    assert drone.observation_count == 3


async def test_list_observations_filters_by_bbox(session: AsyncSession):
    from rid_server.models import Station as StationModel
    await upsert_station(
        session,
        StationModel(
            station_id="station-1", name="S1", lat=0, lon=0,
            alt_m=0, firmware_version="", last_seen=None, online=True,
        ),
    )
    # Two in bbox (Bay Area), one far away (Tokyo)
    for i, (lat, lon) in enumerate(
        [(37.77, -122.41), (37.80, -122.42), (35.68, 139.69)]
    ):
        obs = _obs_model(
            drone_id=f"d-{i}", station_id="station-1", lat=lat, lon=lon
        )
        obs.observation_id = f"obs-{i}"
        await write_observation(session, obs)
    rows = await list_observations(
        session, bbox=(-123.0, 37.0, -122.0, 38.0)
    )
    assert len(rows) == 2
    for r in rows:
        assert -123.0 <= r.position_lon <= -122.0
        assert 37.0 <= r.position_lat <= 38.0


async def test_list_drones_filters_by_recent(session: AsyncSession):
    from datetime import datetime, timedelta, timezone

    from rid_server.models import Station as StationModel
    await upsert_station(
        session,
        StationModel(
            station_id="station-1", name="S1", lat=0, lon=0,
            alt_m=0, firmware_version="", last_seen=None, online=True,
        ),
    )
    now = datetime.now(timezone.utc)
    # 1 fresh, 1 stale (10 minutes ago)
    fresh = _obs_model(drone_id="fresh", station_id="station-1")
    fresh.observation_id = "obs-fresh"
    await write_observation(session, fresh)
    stale = _obs_model(
        drone_id="stale", station_id="station-1",
        received_at=now - timedelta(minutes=10),
    )
    stale.observation_id = "obs-stale"
    await write_observation(session, stale)

    rows = await list_drones(session, online_within_s=60)
    ids = {d.drone_id for d in rows}
    assert "fresh" in ids
    assert "stale" not in ids


async def test_rssi_is_persisted_and_read_back(session: AsyncSession):
    """RSSI used to be dropped on ingest.

    The edge always captured it, but the server had no column, so
    "which station hears this drone best" was unanswerable after the
    fact.  This pins the round trip.
    """
    obs = _obs_model(rssi_dbm=-71)
    await touch_station(session, obs.station_id, online=True)
    await write_observation(session, obs)

    rows = await list_observations(session, limit=10)
    assert len(rows) == 1
    assert rows[0].rssi_dbm == -71


async def test_null_rssi_is_allowed(session: AsyncSession):
    """Old rows and third-party publishers may omit RSSI."""
    obs = _obs_model(rssi_dbm=None)
    await touch_station(session, obs.station_id, online=True)
    await write_observation(session, obs)

    rows = await list_observations(session, limit=10)
    assert rows[0].rssi_dbm is None
