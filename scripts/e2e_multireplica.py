"""Exercise two real API replicas with Redis fan-out and shared MQTT.

This script starts two uvicorn processes against the existing PostgreSQL,
EMQX and Redis services. It then publishes several unidentified-drone
observations through an MQTT shared subscription and verifies:

- each MQTT observation is consumed once across the two replicas;
- every WebSocket client receives the Redis-fanned frame regardless of
  which replica ingested it;
- both replicas read one durable alert row with one trigger event despite
  concurrent rule evaluation;
- readiness and Redis bus metrics are exposed by both replicas.

The test uses unique station/drone/topic/channel values and removes its
database rows at the end.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiomqtt
import asyncpg
import httpx
import websockets
from ulid import ULID

ROOT = Path(__file__).resolve().parent.parent
SERVER_SRC = ROOT / "server" / "src"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _metrics_value(text: str, name: str) -> int:
    for line in text.splitlines():
        if line.startswith(f"{name} "):
            return int(float(line.split(" ", 1)[1]))
    return 0


def _observation(
    *,
    drone_id: str,
    station_id: str,
    seen_at: datetime,
) -> dict:
    return {
        "observation_id": str(ULID()),
        "drone_id": drone_id,
        "operator_id": None,
        "station_id": station_id,
        "protocol": "wifi_nan",
        "rssi_dbm": -61,
        "position": {
            "lat": 22.5405,
            "lon": 113.9345,
            "alt_m": 55.0,
            "accuracy_h_m": 5.0,
            "accuracy_v_m": 8.0,
            "speed_mps": 3.0,
            "heading_deg": 90.0,
        },
        "drone_timestamp": seen_at.isoformat(),
        "received_at": seen_at.isoformat(),
        "self_id": "",
        "operator_location": None,
    }


async def _wait_ready(client: httpx.AsyncClient, url: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    last_error = ""
    while time.monotonic() < deadline:
        try:
            response = await client.get(f"{url}/health/ready")
            if response.status_code == 200:
                return
            last_error = f"{response.status_code}: {response.text[:200]}"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)[:200]
        await asyncio.sleep(0.2)
    raise RuntimeError(f"replica at {url} did not become ready: {last_error}")


async def _wait_mqtt_connected(
    client: httpx.AsyncClient,
    url: str,
    timeout_s: float,
) -> None:
    deadline = time.monotonic() + timeout_s
    last_metrics = ""
    while time.monotonic() < deadline:
        try:
            response = await client.get(f"{url}/metrics")
            response.raise_for_status()
            last_metrics = response.text
            if _metrics_value(last_metrics, "rid_mqtt_connected") == 1:
                return
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.2)
    raise RuntimeError(
        f"replica at {url} did not connect to MQTT: "
        f"{last_metrics[:500]}"
    )


async def _wait_for(
    predicate,
    *,
    timeout_s: float,
    description: str,
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.2)
    raise RuntimeError(f"timed out waiting for {description}")


async def _cleanup_rows(
    *,
    station_id: str,
    drone_id: str,
) -> None:
    conn = await asyncpg.connect(
        host=os.environ.get("RID_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("RID_DB_PORT", "5433")),
        database=os.environ.get("RID_DB_NAME", "rid"),
        user=os.environ.get("RID_DB_USER", "rid"),
        password=os.environ.get("RID_DB_PASSWORD", "rid_dev_password"),
    )
    try:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM notification_outbox WHERE alert_id IN "
                "(SELECT alert_id FROM alerts WHERE drone_id = $1)",
                drone_id,
            )
            await conn.execute(
                "DELETE FROM alerts WHERE drone_id = $1",
                drone_id,
            )
            await conn.execute(
                "DELETE FROM observations WHERE station_id = $1",
                station_id,
            )
            await conn.execute(
                "DELETE FROM drones WHERE drone_id = $1",
                drone_id,
            )
            await conn.execute(
                "DELETE FROM stations WHERE station_id = $1",
                station_id,
            )
    finally:
        await conn.close()


async def main() -> int:
    run = uuid.uuid4().hex[:10]
    station_id = f"MR-{run}"
    drone_id = f"MR-ghost-{run}"
    topic_version = f"v3-{run}"
    shared_group = f"beaconwatch-mr-{run}"
    redis_channel = f"beaconwatch:test:multireplica:{run}"
    ports = [_free_port(), _free_port()]
    urls = [f"http://127.0.0.1:{port}" for port in ports]
    logs: list[Path] = []
    processes: list[subprocess.Popen] = []
    cleanup_error: str | None = None
    completed = False

    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": os.pathsep.join(
                filter(
                    None,
                    [
                        str(SERVER_SRC),
                        env.get("PYTHONPATH", ""),
                    ],
                )
            ),
            "RID_HTTP_HOST": "127.0.0.1",
            "RID_DB_HOST": env.get("RID_DB_HOST", "127.0.0.1"),
            "RID_DB_PORT": env.get("RID_DB_PORT", "5433"),
            "RID_DB_NAME": env.get("RID_DB_NAME", "rid"),
            "RID_DB_USER": env.get("RID_DB_USER", "rid"),
            "RID_DB_PASSWORD": env.get("RID_DB_PASSWORD", "rid_dev_password"),
            "RID_MQTT_HOST": env.get("RID_MQTT_HOST", "127.0.0.1"),
            "RID_MQTT_PORT": env.get("RID_MQTT_PORT", "1883"),
            "RID_MQTT_TLS": "false",
            "RID_MQTT_CLIENT_ID": f"rid-mr-{run}",
            "RID_MQTT_OBS_TOPIC": f"rid/{topic_version}/+/obs",
            "RID_MQTT_INFO_TOPIC": f"rid/{topic_version}/+/info",
            "RID_MQTT_SHARED_GROUP": shared_group,
            "RID_BUS_BACKEND": "redis",
            "RID_REDIS_URL": env.get(
                "RID_TEST_REDIS_URL",
                "redis://127.0.0.1:6379/15",
            ),
            "RID_REDIS_CHANNEL": redis_channel,
            "RID_REDIS_REQUIRED": "true",
            "RID_REDIS_CONNECT_TIMEOUT_S": "2",
            "RID_REDIS_PUBLISH_TIMEOUT_S": "2",
            "RID_REDIS_POLL_TIMEOUT_S": "0.2",
            "RID_REDIS_RECONNECT_MAX_S": "2",
            "RID_API_TOKEN": "",
            "RID_AUTH_USERS_FILE": "",
            "RID_NOTIFY_WEBHOOK_URL": "",
            "RID_NOTIFY_WEBHOOK_SECRET": "",
            "RID_ALERT_MAX_ALT_M": "120",
            "RID_ALERT_UNIDENTIFIED": "true",
            "RID_ALERT_DEDUP_SECONDS": "120",
            "RID_LOG_LEVEL": "WARNING",
            "RID_ENV": "e2e",
        }
    )

    try:
        for index, port in enumerate(ports):
            log_file = tempfile.NamedTemporaryFile(
                prefix=f"beaconwatch-mr-{run}-{index}-",
                suffix=".log",
                delete=False,
            )
            logs.append(Path(log_file.name))
            replica_env = dict(env)
            replica_env["RID_HTTP_PORT"] = str(port)
            replica_env["RID_INSTANCE_ID"] = f"replica-{index}-{run}"
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "uvicorn",
                "rid_server.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
                env=replica_env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            log_file.close()
            processes.append(process)

        async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
            for url in urls:
                await _wait_ready(client, url, timeout_s=25.0)
            for url in urls:
                await _wait_mqtt_connected(client, url, timeout_s=20.0)
        print(f"[1/5] two replicas ready: {', '.join(urls)}")

        observations = [
            _observation(
                drone_id=drone_id,
                station_id=station_id,
                seen_at=datetime.now(timezone.utc)
                + timedelta(milliseconds=index * 10),
            )
            for index in range(8)
        ]
        ws_observations: list[list[dict]] = [[], []]
        ws_alerts: list[list[dict]] = [[], []]
        stop_readers = asyncio.Event()

        async def read_ws(index: int, ws) -> None:
            while not stop_readers.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    return
                frame = json.loads(raw)
                data = frame.get("data") or {}
                if data.get("drone_id") != drone_id:
                    continue
                if frame.get("type") == "obs":
                    ws_observations[index].append(data)
                elif frame.get("type") == "alert":
                    ws_alerts[index].append(data)

        sockets = []
        readers: list[asyncio.Task] = []
        try:
            for index, url in enumerate(urls):
                ws = await websockets.connect(
                    url.replace("http://", "ws://") + "/ws/observations"
                )
                sockets.append(ws)
                readers.append(
                    asyncio.create_task(read_ws(index, ws))
                )
            await asyncio.sleep(0.2)

            async with aiomqtt.Client(
                hostname=env["RID_MQTT_HOST"],
                port=int(env["RID_MQTT_PORT"]),
                identifier=f"rid-mr-publisher-{run}",
            ) as publisher:
                for observation in observations:
                    await publisher.publish(
                        f"rid/{topic_version}/{station_id}/obs",
                        json.dumps(observation).encode(),
                        qos=1,
                    )
            print(f"[2/5] published {len(observations)} observations")

            async def received_total() -> int:
                total = 0
                async with httpx.AsyncClient(
                    timeout=3.0, trust_env=False
                ) as client:
                    for url in urls:
                        response = await client.get(f"{url}/metrics")
                        response.raise_for_status()
                        total += _metrics_value(
                            response.text,
                            "rid_mqtt_received_total",
                        )
                return total

            async def shared_subscription_complete() -> bool:
                return await received_total() == len(observations)

            async def both_ws_complete() -> bool:
                return all(
                    len(items) >= len(observations)
                    for items in ws_observations
                )

            await _wait_for(
                shared_subscription_complete,
                timeout_s=15.0,
                description="exactly-once MQTT consumption across replicas",
            )
            await _wait_for(
                both_ws_complete,
                timeout_s=15.0,
                description="Redis fan-out to both WebSocket clients",
            )
            print(
                "[3/5] shared MQTT consumed once; both replicas "
                "broadcast all frames"
            )

            async def alert_state() -> tuple[dict, list[dict]] | None:
                async with httpx.AsyncClient(
                    timeout=3.0, trust_env=False
                ) as client:
                    response = await client.get(
                        f"{urls[0]}/alerts",
                        params={"drone_id": drone_id, "limit": 10},
                    )
                    response.raise_for_status()
                    rows = response.json()
                    if not rows:
                        return None
                    events = await client.get(
                        f"{urls[0]}/alerts/{rows[0]['alert_id']}/events"
                    )
                    events.raise_for_status()
                    return rows[0], events.json()

            await _wait_for(
                lambda: _alert_exists(alert_state),
                timeout_s=15.0,
                description="durable alert",
            )
            state = await alert_state()
            if state is None:  # pragma: no cover - _wait_for just proved it
                raise RuntimeError("alert disappeared during verification")
            alert, events = state
            if alert["trigger_count"] != 1:
                raise RuntimeError(
                    f"expected one alert trigger, got {alert['trigger_count']}"
                )
            triggered = [
                event for event in events if event["action"] == "triggered"
            ]
            if len(triggered) != 1:
                raise RuntimeError(
                    f"expected one triggered event, got {len(triggered)}"
                )
            if any(len(items) != 1 for items in ws_alerts):
                raise RuntimeError(
                    "duplicate alert WebSocket frames: "
                    f"{[len(items) for items in ws_alerts]}"
                )
            print("[4/5] concurrent replicas produced one alert and one audit event")

            async with httpx.AsyncClient(
                timeout=3.0, trust_env=False
            ) as client:
                for url in urls:
                    ready = await client.get(f"{url}/health/ready")
                    ready.raise_for_status()
                    metrics = await client.get(f"{url}/metrics")
                    metrics.raise_for_status()
                    if "rid_bus_backend_redis 1" not in metrics.text:
                        raise RuntimeError(f"{url} is not using the Redis bus")
                    if "rid_bus_redis_connected 1" not in metrics.text:
                        raise RuntimeError(f"{url} lost its Redis connection")
                    if (
                        _metrics_value(
                            metrics.text,
                            "rid_bus_redis_received_frames_total",
                        )
                        < len(observations)
                    ):
                        raise RuntimeError(
                            f"{url} did not receive all Redis frames"
                        )
            print("[5/5] readiness and Redis bus metrics verified on both replicas")
            completed = True
        finally:
            stop_readers.set()
            for ws in sockets:
                await ws.close()
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
    finally:
        for process in processes:
            if process.returncode is None:
                process.send_signal(signal.SIGTERM)
        for process in processes:
            try:
                await asyncio.wait_for(process.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        try:
            await _cleanup_rows(
                station_id=station_id,
                drone_id=drone_id,
            )
        except Exception as exc:  # noqa: BLE001
            cleanup_error = str(exc)[:300]

        failed = any(process.returncode not in (0, -signal.SIGTERM) for process in processes)
        if failed or cleanup_error or not completed:
            for path in logs:
                print(f"--- {path} ---")
                print(path.read_text(errors="replace")[-8000:])
        for path in logs:
            path.unlink(missing_ok=True)

    if cleanup_error:
        print(f"FAIL: cleanup: {cleanup_error}")
        return 1
    print(
        "PASS: shared MQTT -> two replicas -> Redis fan-out -> "
        "durable cross-replica alert dedup"
    )
    return 0


async def _alert_exists(fetch) -> bool:
    return await fetch() is not None


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
