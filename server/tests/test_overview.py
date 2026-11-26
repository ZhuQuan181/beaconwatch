"""Operational overview and station freshness tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from conftest import make_obs
from pydantic import ValidationError
from rid_server.config import Settings
from rid_server.models import DroneObservation, Position
from rid_server.models import Station as StationModel
from rid_server.repository import (
    get_operational_overview,
    upsert_station,
    write_observation,
)
from sqlalchemy.ext.asyncio import async_sessionmaker


async def _seed_overview(session):
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        await upsert_station(
            s,
            StationModel(
                station_id="fresh",
                name="Fresh",
                lat=0,
                lon=0,
                alt_m=0,
                firmware_version="",
                last_seen=now - timedelta(seconds=5),
                online=True,
            ),
        )
        await upsert_station(
            s,
            StationModel(
                station_id="stale",
                name="Stale",
                lat=0,
                lon=0,
                alt_m=0,
                firmware_version="",
                last_seen=now - timedelta(minutes=8),
                online=True,
            ),
        )
        await upsert_station(
            s,
            StationModel(
                station_id="gone",
                name="Gone",
                lat=0,
                lon=0,
                alt_m=0,
                firmware_version="",
                last_seen=now - timedelta(hours=2),
                online=False,
            ),
        )
        raw = make_obs(drone_id="drone-live", station_id="fresh", received_at=now)
        raw["position"] = Position(**raw["position"])
        await write_observation(s, DroneObservation.model_validate(raw))
    return factory


async def test_overview_counts_stations_and_recent_activity(session):
    await _seed_overview(session)

    overview = await get_operational_overview(
        session,
        window_seconds=3600,
        stale_after_s=120,
        offline_after_s=900,
    )

    assert overview["stations_total"] == 3
    assert overview["stations_online"] == 1
    assert overview["stations_stale"] == 1
    assert overview["stations_offline"] == 1
    assert overview["active_drones"] == 1
    assert overview["observations_window"] == 1
    assert overview["observations_24h"] == 1


def test_offline_threshold_must_cover_stale_threshold():
    with pytest.raises(
        ValidationError,
        match="RID_STATION_OFFLINE_AFTER_S",
    ):
        Settings(station_stale_after_s=600, station_offline_after_s=300)
