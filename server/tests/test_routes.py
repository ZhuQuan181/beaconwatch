"""HTTP route tests via httpx.AsyncClient with ASGITransport."""
from __future__ import annotations

from typing import Any

import httpx
from conftest import make_obs
from rid_server.app import create_app
from rid_server.bus import ObservationBus
from rid_server.mqtt_subscriber import MqttSubscriber
from rid_server.repository import upsert_station, write_observation
from sqlalchemy.ext.asyncio import async_sessionmaker


def _obs_dict(**kw) -> dict[str, Any]:
    return make_obs(**kw)


# ── Health ────────────────────────────────────────────────────────────
async def test_health_ok(db_engine):
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(_settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def _settings():
    from rid_server import config
    return config.get_settings()


# ── Stations ──────────────────────────────────────────────────────────
async def test_get_stations_empty(db_engine):
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(_settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/stations")
    assert r.status_code == 200
    assert r.json() == []


async def test_get_stations_returns_seeded(db_engine):
    from rid_server.models import Station as SM
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        await upsert_station(
            s, SM(
                station_id="S-1", name="Pier", lat=0, lon=0,
                alt_m=0, firmware_version="", last_seen=None, online=True,
            )
        )
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(_settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/stations")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["station_id"] == "S-1"
    assert data[0]["name"] == "Pier"


# ── Observations POST/GET roundtrip via the MQTT path ────────────────
async def test_observation_round_trip(db_engine):
    from rid_server.models import Station as SM
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        await upsert_station(
            s, SM(
                station_id="st-1", name="X", lat=0, lon=0,
                alt_m=0, firmware_version="", last_seen=None, online=True,
            )
        )

    # Insert via repository directly (we test the HTTP POST separately)
    from rid_server.models import DroneObservation, Position
    raw = _obs_dict(drone_id="D-1", station_id="st-1")
    raw["position"] = Position(**raw["position"])
    obs = DroneObservation.model_validate(raw)
    async with factory() as s:
        await write_observation(s, obs)

    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(_settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/observations")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["drone_id"] == "D-1"
    assert data[0]["position"]["lat"] == 37.7749


async def test_observations_bounding_box_filter(db_engine):
    from rid_server.models import DroneObservation, Position
    from rid_server.models import Station as SM
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        await upsert_station(
            s, SM(
                station_id="st-1", name="X", lat=0, lon=0,
                alt_m=0, firmware_version="", last_seen=None, online=True,
            )
        )
    for i, (lat, lon) in enumerate([(37.77, -122.41), (35.68, 139.69)]):
        raw = _obs_dict(
            drone_id=f"d-{i}", station_id="st-1", lat=lat, lon=lon
        )
        raw["observation_id"] = f"obs-{i}"
        raw["position"] = Position(**raw["position"])
        obs = DroneObservation.model_validate(raw)
        async with factory() as s:
            await write_observation(s, obs)

    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(_settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get(
            "/observations",
            params={"bbox": "-123,37,-122,38"},
        )
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["drone_id"] == "d-0"


async def test_observations_invalid_bbox_422(db_engine):
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(_settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/observations", params={"bbox": "not-a-bbox"})
    assert r.status_code == 422


# ── Drones ────────────────────────────────────────────────────────────
async def test_get_drones(db_engine):
    from rid_server.models import DroneObservation, Position
    from rid_server.models import Station as SM
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        await upsert_station(
            s, SM(
                station_id="st-1", name="X", lat=0, lon=0,
                alt_m=0, firmware_version="", last_seen=None, online=True,
            )
        )
    raw = _obs_dict(drone_id="D-Z", station_id="st-1")
    raw["position"] = Position(**raw["position"])
    obs = DroneObservation.model_validate(raw)
    async with factory() as s:
        await write_observation(s, obs)

    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(_settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/drones")
    assert r.status_code == 200
    data = r.json()
    assert len(data) >= 1
    assert data[0]["drone_id"] == "D-Z"
    assert data[0]["observation_count"] == 1


async def test_get_drone_by_id_404_when_missing(db_engine):
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(_settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/drones/does-not-exist")
    assert r.status_code == 404


# ── Timeline window + unidentified filter ─────────────────────────────
async def _seed_three_observations(db_engine):
    """Three obs: two identified at different times, one never identifies."""
    from rid_server.models import DroneObservation, Position
    from rid_server.models import Station as SM
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        await upsert_station(
            s, SM(station_id="st-1", name="X", lat=0, lon=0,
                  alt_m=0, firmware_version="", last_seen=None, online=True)
        )

    rows = [
        ("obs-early", "drone-good", "OP-1", "2026-11-14T10:00:00+00:00"),
        ("obs-mid", "drone-good", "OP-1", "2026-11-14T11:00:00+00:00"),
        ("obs-anon", "drone-ghost", None, "2026-11-14T11:30:00+00:00"),
    ]
    for oid, drone, op, ts in rows:
        raw = _obs_dict(drone_id=drone, station_id="st-1")
        raw["observation_id"] = oid
        raw["operator_id"] = op
        raw["received_at"] = ts
        raw["drone_timestamp"] = ts
        raw["position"] = Position(**raw["position"])
        async with factory() as s:
            await write_observation(s, DroneObservation.model_validate(raw))
    return factory


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_observations_absolute_time_window(db_engine):
    """A scrubber needs absolute from/to, not just since_seconds."""
    await _seed_three_observations(db_engine)
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(_settings())

    async with _client(app) as c:
        r = await c.get("/observations", params={
            "from": "2026-11-14T10:30:00Z",
            "to": "2026-11-14T11:15:00Z",
        })
    assert r.status_code == 200
    ids = [o["observation_id"] for o in r.json()]
    assert ids == ["obs-mid"], ids


async def test_observations_rejects_inverted_window(db_engine):
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(_settings())
    async with _client(app) as c:
        r = await c.get("/observations", params={
            "from": "2026-11-14T12:00:00Z",
            "to": "2026-11-14T11:00:00Z",
        })
    assert r.status_code == 400


async def test_observations_unidentified_only(db_engine):
    """The 'find the ghost drone' filter."""
    await _seed_three_observations(db_engine)
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(_settings())

    async with _client(app) as c:
        r = await c.get("/observations", params={"unidentified_only": "true"})
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["drone_id"] == "drone-ghost"


async def test_unidentified_drones_endpoint(db_engine):
    await _seed_three_observations(db_engine)
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(_settings())

    async with _client(app) as c:
        r = await c.get("/drones/unidentified")
    assert r.status_code == 200
    assert r.json() == ["drone-ghost"]


async def test_rssi_appears_in_api_response(db_engine):
    """RSSI must survive ingest→DB→HTTP, not just DB."""
    await _seed_three_observations(db_engine)
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(_settings())

    async with _client(app) as c:
        r = await c.get("/observations", params={"limit": 1})
    assert r.status_code == 200
    assert r.json()[0]["rssi_dbm"] == -62
