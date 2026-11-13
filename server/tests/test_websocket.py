"""WebSocket end-to-end tests: publish to bus → client receives."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import websockets

from rid_server.app import create_app
from rid_server.bus import ObservationBus
from rid_server.mqtt_subscriber import MqttSubscriber

from conftest import make_obs


async def test_websocket_receives_published_observation(
    db_engine, _sqlite_settings, unused_tcp_port: int
):
    """Spin up the FastAPI app on a real port, connect a websocket client,
    publish to the bus, assert the client receives it.

    We use a real port because Starlette's TestClient doesn't support
    WebSockets directly; websockets.connect + uvicorn is the standard
    workaround.
    """
    import uvicorn

    app = create_app()
    sub = MqttSubscriber(_sqlite_settings)
    app.state.subscriber = sub
    # NOTE: don't pre-set app.state.bus — the lifespan creates one.  We
    # will publish through that one after server start.

    config = uvicorn.Config(
        app, host="127.0.0.1", port=unused_tcp_port, log_level="warning"
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    try:
        # Wait for server to come up
        for _ in range(50):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started, "uvicorn did not start"

        # The lifespan-populated bus
        bus: ObservationBus = app.state.bus

        # Connect websocket client
        async with websockets.connect(
            f"ws://127.0.0.1:{unused_tcp_port}/ws/observations"
        ) as ws:
            # Give the server a tick to register the subscriber
            await asyncio.sleep(0.1)

            # Publish an observation through the bus
            from rid_server.models import DroneObservation, Position
            raw = make_obs(drone_id="D-WS", station_id="st-1")
            raw["position"] = Position(**raw["position"])
            obs = DroneObservation.model_validate(raw)
            await bus.publish(obs)

            # Receive within 1s
            msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
            data = json.loads(msg)
            assert data["type"] == "obs"
            assert data["data"]["drone_id"] == "D-WS"
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=2.0)
        await sub.stop()


async def test_websocket_slow_consumer_does_not_block_publisher(
    db_engine, _sqlite_settings,
):
    """If a client doesn't read, we drop oldest; the bus must stay unblocked."""
    bus = ObservationBus(max_queue_per_client=4)

    # Slow subscriber (never reads) + fast subscriber (reads everything)
    slow_q = asyncio.Queue(maxsize=4)
    # Fast consumer never blocks (we use a large enough queue to hold
    # every published item — the contract is "no item loss on healthy
    # consumers", not "consumer always has unlimited capacity").
    fast_q = asyncio.Queue(maxsize=1024)

    from rid_server.models import DroneObservation, Position
    raw = make_obs(drone_id="D-X", station_id="st-1")
    raw["position"] = Position(**raw["position"])
    obs = DroneObservation.model_validate(raw)

    # Mimic the bus subscribe by registering queues directly
    bus._subscribers.append(slow_q)
    bus._subscribers.append(fast_q)

    # Fill slow first
    for _ in range(3):
        await bus.publish(obs)
    assert slow_q.qsize() == 3
    assert fast_q.qsize() == 3

    # Now overflow slow → should evict oldest, fast should still get all
    for _ in range(3):
        await bus.publish(obs)
    # Slow consumer stays bounded (eviction keeps the newest item)
    assert slow_q.qsize() <= 4
    # Fast consumer got every single item because we never block on it
    assert fast_q.qsize() == 6
    # The newest item the slow queue holds is the most recent publish
    assert slow_q._queue[-1] is obs or slow_q._queue[-1].observation_id == obs.observation_id


@pytest.fixture
def unused_tcp_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
