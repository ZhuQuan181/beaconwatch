"""End-to-end chain check: edge → MQTT → server → DB → HTTP.

This is the script `make e2e` should have been running.  It does NOT
mock anything: real aiomqtt publish, real EMQX broker, real
MqttSubscriber, real Postgres insert, real FastAPI route.

Steps and what each one proves:
  1. edge builds a RidObservation and serialises it     — edge is importable
  2. publish to rid/v1/<station>/obs over EMQX          — broker reachable
  3. server subscriber parses it                        — WIRE CONTRACT holds
  4. row lands in Postgres                              — persistence works
  5. GET /observations returns it with rssi             — API surfaces it

Usage:
    RID_DB_PORT=5433 RID_MQTT_HOST=127.0.0.1 python scripts/e2e_chain.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for pkg in ("edge/src", "server/src"):
    sys.path.insert(0, str(ROOT / pkg))

import aiomqtt  # noqa: E402

from rid_edge.models import Position as EdgePosition  # noqa: E402
from rid_edge.models import Protocol as EdgeProtocol  # noqa: E402
from rid_edge.models import RidObservation  # noqa: E402
from rid_server.config import get_settings  # noqa: E402
from rid_server.mqtt_subscriber import MqttSubscriber  # noqa: E402

STATION = "SZ-Nanshan-01"


def _build(tag: str, operator_id: str | None) -> RidObservation:
    return RidObservation(
        observation_id=f"e2e-{tag}-{uuid.uuid4().hex[:12]}",
        drone_id=f"E2E-{tag.upper()}",
        operator_id=operator_id,
        protocol=EdgeProtocol.ASTM_F3411,
        station_id=STATION,
        rssi_dbm=-63,
        position=EdgePosition(
            lat=22.5405,
            lon=113.9345,
            alt_m=118.0,
            accuracy_h_m=4.0,
            accuracy_v_m=6.0,
            speed_mps=14.2,
            heading_deg=47.0,
        ),
        drone_timestamp=datetime.now(timezone.utc),
        self_id=f"e2e {tag} mission",
    )


async def main() -> int:
    settings = get_settings()
    # Unique client ID per run.  MQTT brokers allow one connection per
    # client ID — reuse the default "rid-server" and the broker kicks
    # whichever connection came first, producing a reconnect storm where
    # neither subscriber ever sees a message.
    #
    # Also: publish on a run-scoped topic version.  A long-lived
    # production container may still be subscribed to rid/v1/+/obs with
    # older code; both subscribers would receive the same message, and
    # on a primary-key conflict the first writer wins — so the row in
    # the DB can be the OLD schema's row, making rssi look NULL.  A
    # private topic keeps the verification honest without touching
    # anything that's already running.
    run = uuid.uuid4().hex[:8]
    topic_version = f"v2-{run}"
    settings = settings.model_copy(
        update={
            "mqtt_client_id": f"rid-e2e-{run}",
            "mqtt_obs_topic": f"rid/{topic_version}/+/obs",
        }
    )
    topic = f"rid/{topic_version}/{STATION}/obs"
    identified = _build(f"good{run}", "OP-E2E-1")
    ghost = _build(f"ghost{run}", None)

    print(f"[1/5] built 2 observations (run={run})")

    received: list[str] = []
    errors: list[str] = []

    async def on_persisted(obs) -> None:
        received.append(obs.observation_id)

    sub = MqttSubscriber(settings)
    sub.on_persisted = on_persisted

    async with aiomqtt.Client(
        hostname=settings.mqtt_host, port=settings.mqtt_port
    ) as pub:
        print(f"[2/5] connected to MQTT {settings.mqtt_host}:{settings.mqtt_port}")

        # Subscribe first so we don't miss our own messages.
        async def run_sub():
            try:
                await sub.start()
            except Exception as e:  # noqa: BLE001
                errors.append(f"subscriber: {e}")

        sub_task = asyncio.create_task(run_sub())
        await asyncio.sleep(1.5)

        for obs in (identified, ghost):
            await pub.publish(topic, obs.to_mqtt_payload(), qos=1)
            print(f"[3/5] published {obs.observation_id} → {topic}")

        # Give the subscriber time to persist both.
        for _ in range(40):
            await asyncio.sleep(0.25)
            if len(received) >= 2:
                break
        sub_task.cancel()
        try:
            await sub_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    print(f"[4/5] subscriber persisted {len(received)}/2: {received}")
    if errors:
        for e in errors:
            print(f"      ✗ {e}")
    if len(received) < 2:
        print("FAIL: messages did not reach the database")
        return 1

    # 5) Read back over HTTP.
    import httpx

    from rid_server.app import create_app

    app = create_app()
    app.state.bus = None
    app.state.subscriber = sub
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/observations", params={"limit": 50})
        r.raise_for_status()
        rows = [o for o in r.json() if o["observation_id"] in received]
        ghost_ids = await c.get("/drones/unidentified")

    print(f"[5/5] HTTP returned {len(rows)}/2 of our rows")
    for o in rows:
        print(
            f"      {o['observation_id']}  drone={o['drone_id']}  "
            f"op={o['operator_id']}  rssi={o.get('rssi_dbm')}  "
            f"speed={o['position']['speed_mps']}"
        )

    ok = True
    if len(rows) != 2:
        print("FAIL: HTTP did not return both rows")
        ok = False
    missing_rssi = [o for o in rows if o.get("rssi_dbm") is None]
    if missing_rssi:
        print("FAIL: rssi lost between ingest and HTTP")
        ok = False
    missing_speed = [o for o in rows if not o["position"]["speed_mps"]]
    if missing_speed:
        print("FAIL: speed_mps lost (the original bug)")
        ok = False
    if ghost.drone_id not in ghost_ids.json():
        print(f"FAIL: ghost drone {ghost.drone_id} not listed as unidentified")
        ok = False

    print("PASS: edge → MQTT → server → DB → HTTP verified" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
