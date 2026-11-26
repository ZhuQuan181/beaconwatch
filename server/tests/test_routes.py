"""HTTP route tests via httpx.AsyncClient with ASGITransport."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from conftest import make_obs
from rid_server.app import create_app
from rid_server.bus import ObservationBus
from rid_server.config import Settings
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


async def test_health_live_and_ready(db_engine):
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(_settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        live = await c.get("/health/live")
        ready = await c.get("/health/ready")
        healthz = await c.get("/healthz")
    assert live.status_code == 200
    assert ready.status_code == 200
    readiness = ready.json()
    assert readiness["status"] == "ready"
    assert healthz.status_code == 200


async def test_metrics_exposes_ingest_counters(db_engine):
    app = create_app()
    app.state.bus = ObservationBus()
    subscriber = MqttSubscriber(_settings())
    subscriber.received = 7
    subscriber.persisted = 6
    subscriber.invalid = 1
    subscriber.station_infos = 2
    app.state.subscriber = subscriber
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/metrics")
    assert r.status_code == 200
    assert "rid_mqtt_received_total 7" in r.text
    assert "rid_mqtt_persisted_total 6" in r.text
    assert "rid_station_info_total 2" in r.text


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


async def test_overview_endpoint_returns_health_summary(db_engine):
    await _seed_three_observations(
        db_engine,
        anchor=datetime.now(timezone.utc),
    )
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(_settings())

    async with _client(app) as c:
        r = await c.get("/stats/overview")

    assert r.status_code == 200
    body = r.json()
    assert body["stations_total"] == 1
    assert body["observations_24h"] == 3
    assert body["window_seconds"] == 3600


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
    raw["operator_location"] = {
        "lat": 37.775,
        "lon": -122.42,
        "alt_m": 12.0,
    }
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
    assert data[0]["position"]["accuracy_h_m"] == 5.0
    assert data[0]["position"]["accuracy_v_m"] == 8.0
    assert data[0]["operator_location"]["lat"] == 37.775
    assert data[0]["operator_location"]["alt_m"] == 12.0


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
async def _seed_three_observations(
    db_engine,
    *,
    anchor: datetime | None = None,
):
    """Three obs: two identified at different times, one never identifies."""
    from rid_server.models import DroneObservation, Position
    from rid_server.models import Station as SM
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        await upsert_station(
            s, SM(station_id="st-1", name="X", lat=0, lon=0,
                  alt_m=0, firmware_version="", last_seen=None, online=True)
        )

    if anchor is None:
        rows = [
            ("obs-early", "drone-good", "OP-1", "2026-11-14T10:00:00+00:00"),
            ("obs-mid", "drone-good", "OP-1", "2026-11-14T11:00:00+00:00"),
            ("obs-anon", "drone-ghost", None, "2026-11-14T11:30:00+00:00"),
        ]
    else:
        # Keep default-window tests independent of the calendar date.
        rows = [
            (
                "obs-early",
                "drone-good",
                "OP-1",
                (anchor - timedelta(minutes=90)).isoformat(),
            ),
            (
                "obs-mid",
                "drone-good",
                "OP-1",
                (anchor - timedelta(minutes=30)).isoformat(),
            ),
            (
                "obs-anon",
                "drone-ghost",
                None,
                (anchor - timedelta(minutes=5)).isoformat(),
            ),
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
    await _seed_three_observations(
        db_engine,
        anchor=datetime.now(timezone.utc),
    )
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


async def test_export_csv_respects_unidentified_filter(db_engine):
    await _seed_three_observations(db_engine)
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(_settings())

    async with _client(app) as c:
        r = await c.get(
            "/observations/export",
            params={"unidentified_only": "true"},
        )

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["x-export-rows"] == "1"
    assert r.headers["x-export-truncated"] == "false"
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert [row["observation_id"] for row in rows] == ["obs-anon"]
    assert rows[0]["operator_id"] == ""


async def test_export_geojson_is_feature_collection(db_engine):
    await _seed_three_observations(db_engine)
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(_settings())

    async with _client(app) as c:
        r = await c.get(
            "/observations/export",
            params={"format": "geojson"},
        )

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/geo+json")
    body = r.json()
    assert body["type"] == "FeatureCollection"
    assert len(body["features"]) == 3
    first = body["features"][0]
    assert first["geometry"]["type"] == "Point"
    assert len(first["geometry"]["coordinates"]) == 3
    assert first["properties"]["observation_id"] == "obs-anon"


async def test_export_is_capped_by_server_policy(db_engine):
    await _seed_three_observations(db_engine)
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(_settings())
    app.state.settings = Settings()
    app.state.settings.export_max_rows = 2

    async with _client(app) as c:
        r = await c.get("/observations/export", params={"limit": 100})

    assert r.status_code == 200
    assert r.headers["x-export-rows"] == "2"
    assert r.headers["x-export-truncated"] == "true"


# ── Operational hardening ─────────────────────────────────────────────
async def test_security_headers_and_request_id(db_engine):
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(_settings())
    async with _client(app) as c:
        r = await c.get("/health", headers={"X-Request-ID": "trace-123"})
    assert r.status_code == 200
    assert r.headers["x-request-id"] == "trace-123"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"


async def test_optional_api_token_protects_data_not_health(db_engine):
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(_settings())
    app.state.settings = Settings(api_token="s3cret")

    async with _client(app) as c:
        assert (await c.get("/health")).status_code == 200
        unauthorized = await c.get("/stations")
        assert unauthorized.status_code == 401
        assert unauthorized.headers["www-authenticate"] == "Bearer"
        authorized = await c.get(
            "/stations",
            headers={"Authorization": "Bearer s3cret"},
        )
    assert authorized.status_code == 200


async def test_observations_cursor_pagination(db_engine):
    await _seed_three_observations(db_engine)
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(_settings())

    async with _client(app) as c:
        first = await c.get("/observations", params={"limit": 1})
        assert first.status_code == 200
        assert [row["observation_id"] for row in first.json()] == ["obs-anon"]
        cursor = first.headers["x-next-cursor"]

        second = await c.get(
            "/observations",
            params={"limit": 1, "cursor": cursor},
        )
        assert second.status_code == 200
        assert [row["observation_id"] for row in second.json()] == ["obs-mid"]
        second_cursor = second.headers["x-next-cursor"]

        third = await c.get(
            "/observations",
            params={"limit": 1, "cursor": second_cursor},
        )
    assert [row["observation_id"] for row in third.json()] == ["obs-early"]
    assert "x-next-cursor" not in third.headers


async def test_observations_page_endpoint_returns_cursor(db_engine):
    await _seed_three_observations(db_engine)
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(_settings())

    async with _client(app) as c:
        first = await c.get("/observations/page", params={"limit": 2})
        assert first.status_code == 200
        body = first.json()
        assert [row["observation_id"] for row in body["items"]] == [
            "obs-anon",
            "obs-mid",
        ]
        assert body["next_cursor"]

        second = await c.get(
            "/observations/page",
            params={"limit": 2, "cursor": body["next_cursor"]},
        )
    assert [row["observation_id"] for row in second.json()["items"]] == [
        "obs-early"
    ]
    assert second.json()["next_cursor"] is None


async def test_invalid_cursor_is_400(db_engine):
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(_settings())
    async with _client(app) as c:
        r = await c.get("/observations", params={"cursor": "not-a-cursor"})
    assert r.status_code == 400


async def test_metrics_include_request_and_websocket_counters(db_engine):
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(_settings())
    async with _client(app) as c:
        await c.get("/health")
        r = await c.get("/metrics")
    assert r.status_code == 200
    assert "rid_http_requests_total" in r.text
    assert "rid_http_requests_in_flight" in r.text
    assert (
        'rid_http_responses_total{method="GET",route="/health",status="200"} 1'
        in r.text
    )
    assert "rid_ws_dropped_frames_total 0" in r.text
