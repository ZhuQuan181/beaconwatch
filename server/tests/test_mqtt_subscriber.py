"""Tests for the MQTT subscriber's per-message handler.

We don't need a real broker — :meth:`MqttSubscriber.handle_message`
takes (topic, payload) and is fully testable as a unit.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from conftest import make_obs
from rid_server.mqtt_subscriber import MqttSubscriber
from rid_server.repository import (
    list_drones,
    upsert_station,
)
from sqlalchemy.ext.asyncio import async_sessionmaker


async def test_extract_station_id_valid():
    assert MqttSubscriber._extract_station_id("rid/v1/station-42/obs") == "station-42"
    assert MqttSubscriber._extract_station_id("rid/v1/abc/obs") == "abc"


async def test_extract_station_id_accepts_other_versions():
    """Topic version must not be pinned to v1.

    The e2e harness runs a second subscriber on rid/v2-<run>/+/obs so
    it doesn't collide with a long-lived production subscriber on the
    same topic.  When this method hard-coded "v1", every message on the
    private topic arrived with station_id=None and was filed under the
    wrong station.
    """
    assert MqttSubscriber._extract_station_id("rid/v2/drone-lab/obs") == "drone-lab"
    assert MqttSubscriber._extract_station_id("rid/v2-abc123/lab/obs") == "lab"
    assert MqttSubscriber._extract_station_id("rid/v10/x/obs") == "x"


async def test_extract_station_id_invalid():
    # Wrong number of segments
    assert MqttSubscriber._extract_station_id("rid/v1/obs") is None
    assert MqttSubscriber._extract_station_id("") is None
    assert MqttSubscriber._extract_station_id("not/a/topic") is None
    # Unversioned topic — the segment after "rid" must look like a version.
    assert MqttSubscriber._extract_station_id("rid/station/obs") is None
    assert MqttSubscriber._extract_station_id("other/v1/station/obs") is None


async def test_handle_message_persists_and_counts(db_engine, _sqlite_settings):
    # Seed station
    from rid_server.models import Station as SM
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        await upsert_station(
            s, SM(
                station_id="station-1", name="X", lat=0, lon=0,
                alt_m=0, firmware_version="", last_seen=None, online=True,
            )
        )

    sub = MqttSubscriber(_sqlite_settings)
    obs = make_obs(drone_id="D-1", station_id="station-1")
    payload = json.dumps(obs).encode()

    fake_message = MagicMock()
    fake_message.topic = "rid/v1/station-1/obs"
    fake_message.payload = payload

    await sub.handle_message(fake_message.topic, fake_message.payload)

    assert sub.received == 1
    assert sub.persisted == 1
    assert sub.invalid == 0

    async with factory() as s:
        drones = await list_drones(s, online_within_s=3600)
        assert len(drones) == 1
        assert drones[0].drone_id == "D-1"


async def test_handle_message_invalid_json_increments_invalid(
    db_engine, _sqlite_settings
):
    sub = MqttSubscriber(_sqlite_settings)
    await sub.handle_message("rid/v1/station-1/obs", b"not json {")
    assert sub.invalid == 1
    assert sub.persisted == 0


async def test_handle_message_missing_fields_increments_invalid(
    db_engine, _sqlite_settings
):
    sub = MqttSubscriber(_sqlite_settings)
    bad = make_obs(drone_id="D-1", station_id="station-1")
    bad.pop("position")  # required
    await sub.handle_message("rid/v1/station-1/obs", json.dumps(bad).encode())
    assert sub.invalid == 1


async def test_handle_message_topic_station_id_overrides_payload(
    db_engine, _sqlite_settings
):
    """Trust the topic station_id, not the payload's (security)."""
    from rid_server.models import Station as SM
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        # Seed BOTH station-1 and station-2
        for sid in ("station-1", "station-2"):
            await upsert_station(
                s, SM(
                    station_id=sid, name=sid, lat=0, lon=0,
                    alt_m=0, firmware_version="", last_seen=None, online=True,
                )
            )

    sub = MqttSubscriber(_sqlite_settings)
    obs = make_obs(drone_id="D-X", station_id="station-1")
    obs["observation_id"] = "obs-1"
    await sub.handle_message(
        "rid/v1/station-2/obs", json.dumps(obs).encode()
    )
    assert sub.persisted == 1

    # The persisted row should be attributed to station-2
    from rid_server.db import Observation
    from sqlalchemy import select
    async with factory() as s:
        rows = (
            await s.execute(select(Observation))
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].station_id == "station-2"


async def test_handle_message_two_messages_same_drone_increments_count(
    db_engine, _sqlite_settings
):
    from rid_server.models import Station as SM
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        await upsert_station(
            s, SM(
                station_id="station-1", name="X", lat=0, lon=0,
                alt_m=0, firmware_version="", last_seen=None, online=True,
            )
        )

    sub = MqttSubscriber(_sqlite_settings)
    for i in range(2):
        obs = make_obs(drone_id="D-Y", station_id="station-1")
        obs["observation_id"] = f"obs-y-{i}"
        await sub.handle_message(
            "rid/v1/station-1/obs", json.dumps(obs).encode()
        )

    assert sub.persisted == 2
    async with factory() as s:
        drones = await list_drones(s, online_within_s=3600)
        assert drones[0].observation_count == 2
