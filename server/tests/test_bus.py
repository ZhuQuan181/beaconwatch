"""Live bus backend tests, including optional real-Redis fan-out."""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import httpx
import pytest
from conftest import make_obs
from rid_server.app import create_app
from rid_server.bus import ObservationBus, RedisObservationBus, create_bus
from rid_server.config import Settings
from rid_server.models import DroneObservation, Position
from rid_server.mqtt_subscriber import MqttSubscriber


def _observation(drone_id: str = "bus-drone") -> DroneObservation:
    raw = make_obs(drone_id=drone_id, station_id="bus-station")
    raw["position"] = Position(**raw["position"])
    return DroneObservation.model_validate(raw)


async def _redis_available(url: str) -> bool:
    try:
        import redis.asyncio as redis_asyncio

        client = redis_asyncio.Redis.from_url(url, decode_responses=True)
        try:
            await client.ping()
        finally:
            await client.aclose()
        return True
    except Exception:  # noqa: BLE001
        return False


def test_create_bus_defaults_to_memory():
    assert isinstance(create_bus(Settings()), ObservationBus)


def test_instance_id_defaults_when_compose_supplies_empty_value():
    first = Settings(instance_id="", mqtt_client_id="api")
    second = Settings(instance_id="", mqtt_client_id="api")

    assert first.instance_id
    assert second.instance_id
    assert first.mqtt_client_identifier.startswith("api-")
    assert second.mqtt_client_identifier.startswith("api-")


def test_mqtt_shared_subscription_topics():
    plain = Settings()
    shared = Settings(mqtt_shared_group="beaconwatch-prod")

    assert plain.mqtt_obs_subscription_topic == "rid/v1/+/obs"
    assert plain.mqtt_info_subscription_topic == "rid/v1/+/info"
    assert (
        shared.mqtt_obs_subscription_topic
        == "$share/beaconwatch-prod/rid/v1/+/obs"
    )
    assert (
        shared.mqtt_info_subscription_topic
        == "$share/beaconwatch-prod/rid/v1/+/info"
    )


def test_invalid_mqtt_shared_group_is_rejected():
    with pytest.raises(ValueError, match="RID_MQTT_SHARED_GROUP"):
        Settings(mqtt_shared_group="invalid/group")


def test_invalid_alert_dedup_window_is_rejected():
    with pytest.raises(ValueError):
        Settings(alert_dedup_seconds=-1)


async def test_memory_bus_delivers_to_local_subscriber():
    bus = ObservationBus()
    async with bus.subscribe() as queue:
        await bus.publish(_observation())
        frame = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert frame["kind"] == "obs"
    assert frame["data"]["drone_id"] == "bus-drone"


async def test_redis_bus_fans_out_across_two_instances():
    redis_url = os.environ.get(
        "RID_TEST_REDIS_URL",
        "redis://127.0.0.1:6379/15",
    )
    if not await _redis_available(redis_url):
        pytest.skip("real Redis is not available")

    channel = f"beaconwatch:test:{uuid.uuid4().hex}"
    settings = Settings(
        bus_backend="redis",
        redis_url=redis_url,
        redis_channel=channel,
        redis_connect_timeout_s=1.0,
        redis_publish_timeout_s=1.0,
        redis_reconnect_max_s=1.0,
    )
    bus_a = RedisObservationBus(settings)
    bus_b = RedisObservationBus(settings)
    try:
        await bus_a.start()
        await bus_b.start()
        async with bus_b.subscribe() as queue:
            await bus_a.publish(_observation("cross-replica"))
            frame = await asyncio.wait_for(queue.get(), timeout=2.0)
        assert frame["kind"] == "obs"
        assert frame["data"]["drone_id"] == "cross-replica"
        assert bus_a.backend == "redis"
        assert bus_a.redis_connected
        assert bus_b.remote_received_frames >= 1
    finally:
        await bus_a.stop()
        await bus_b.stop()


async def test_redis_unavailable_falls_back_to_local_fanout():
    settings = Settings(
        bus_backend="redis",
        redis_url="redis://127.0.0.1:1/0",
        redis_channel="beaconwatch:test:unreachable",
        redis_connect_timeout_s=0.2,
        redis_publish_timeout_s=0.2,
        redis_poll_timeout_s=0.05,
        redis_reconnect_max_s=1.0,
    )
    bus = RedisObservationBus(settings)
    try:
        await bus.start()
        async with bus.subscribe() as queue:
            await bus.publish(_observation("degraded"))
            frame = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert frame["data"]["drone_id"] == "degraded"
        assert bus.redis_connected is False
        assert bus.publish_failures >= 1
    finally:
        await bus.stop()


async def test_required_redis_starts_unready_instead_of_crashing():
    settings = Settings(
        bus_backend="redis",
        redis_url="redis://127.0.0.1:1/0",
        redis_channel="beaconwatch:test:unreachable",
        redis_connect_timeout_s=0.2,
        redis_publish_timeout_s=0.2,
        redis_poll_timeout_s=0.05,
        redis_reconnect_max_s=1.0,
        redis_required=True,
    )
    bus = RedisObservationBus(settings)
    try:
        await bus.start()
        assert bus.redis_connected is False
    finally:
        await bus.stop()


def test_invalid_bus_backend_is_rejected():
    with pytest.raises(ValueError, match="RID_BUS_BACKEND"):
        Settings(bus_backend="kafka")


async def test_redis_required_returns_not_ready(db_engine):
    settings = Settings(
        bus_backend="redis",
        redis_url="redis://127.0.0.1:1/0",
        redis_required=True,
    )
    app = create_app()
    app.state.settings = settings
    app.state.bus = RedisObservationBus(settings)
    app.state.subscriber = MqttSubscriber(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        ready = await client.get("/health/ready")
        metrics = await client.get("/metrics")

    assert ready.status_code == 503
    assert ready.json()["detail"] == "redis bus unavailable"
    assert "rid_bus_backend_redis 1" in metrics.text
    assert "rid_bus_redis_connected 0" in metrics.text


async def test_app_lifespan_starts_and_stops_configured_bus(
    db_engine, monkeypatch
):
    settings = Settings(
        bus_backend="redis",
        redis_url="redis://127.0.0.1:1/0",
        redis_connect_timeout_s=0.1,
        redis_poll_timeout_s=0.05,
    )
    monkeypatch.setattr("rid_server.app.get_settings", lambda: settings)
    app = create_app()

    async with app.router.lifespan_context(app):
        bus = app.state.bus
        assert isinstance(bus, RedisObservationBus)
        assert bus.backend == "redis"
        assert bus.redis_connected is False

    assert bus._listen_task is None


class _FakePubSub:
    def __init__(self) -> None:
        self._messages: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.channel = channel

    async def get_message(
        self,
        *,
        ignore_subscribe_messages: bool = False,
        timeout: float | None = None,
    ) -> dict[str, object] | None:
        assert ignore_subscribe_messages
        try:
            return await asyncio.wait_for(
                self._messages.get(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return None

    async def aclose(self) -> None:
        self.closed = True

    def deliver(self, payload: bytes) -> None:
        self._messages.put_nowait({"type": "message", "data": payload})


class _FakeRedis:
    def __init__(self) -> None:
        self.pubsubs: list[_FakePubSub] = []
        self.fail_publish = False

    def pubsub(self, *, ignore_subscribe_messages: bool = False) -> _FakePubSub:
        assert ignore_subscribe_messages
        pubsub = _FakePubSub()
        self.pubsubs.append(pubsub)
        return pubsub

    async def publish(self, channel: str, payload: bytes) -> int:
        if self.fail_publish:
            raise ConnectionError("injected Redis publish failure")
        for pubsub in self.pubsubs:
            if not pubsub.closed:
                pubsub.deliver(payload)
        return len(self.pubsubs)

    async def aclose(self) -> None:
        for pubsub in self.pubsubs:
            pubsub.closed = True


async def test_redis_publish_failure_reconnects_and_resumes_fanout():
    settings = Settings(
        bus_backend="redis",
        redis_channel="beaconwatch:test:fake",
        redis_connect_timeout_s=0.2,
        redis_publish_timeout_s=0.2,
        redis_poll_timeout_s=0.05,
        redis_reconnect_max_s=1.0,
    )
    client = _FakeRedis()
    bus = RedisObservationBus(settings, client=client)
    try:
        await bus.start()
        assert bus.redis_connected
        async with bus.subscribe() as queue:
            client.fail_publish = True
            await bus.publish(_observation("local-fallback"))
            local = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert local["data"]["drone_id"] == "local-fallback"
            assert bus.redis_connected is False

            for _ in range(100):
                if bus.redis_connected and len(client.pubsubs) >= 2:
                    break
                await asyncio.sleep(0.02)
            assert bus.redis_connected
            assert len(client.pubsubs) >= 2

            client.fail_publish = False
            await bus.publish(_observation("recovered"))
            recovered = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert recovered["data"]["drone_id"] == "recovered"
            assert bus.publish_failures == 1
            assert bus.reconnects >= 1
            assert bus.remote_received_frames >= 1
    finally:
        await bus.stop()


async def test_redis_bus_ignores_malformed_remote_frames():
    settings = Settings(
        bus_backend="redis",
        redis_channel="beaconwatch:test:malformed",
        redis_poll_timeout_s=0.05,
    )
    client = _FakeRedis()
    bus = RedisObservationBus(settings, client=client)
    try:
        await bus.start()
        pubsub = client.pubsubs[0]
        async with bus.subscribe() as queue:
            pubsub.deliver(b"\xff")
            pubsub.deliver(b"not-json")
            pubsub.deliver(
                json.dumps({"kind": "unknown", "data": {}}).encode()
            )
            pubsub.deliver(
                json.dumps(
                    {
                        "kind": "obs",
                        "data": _observation().model_dump(mode="json"),
                    }
                ).encode()
            )
            frame = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert frame["kind"] == "obs"
        assert bus.remote_received_frames == 1
        assert bus.redis_connected
    finally:
        await bus.stop()
