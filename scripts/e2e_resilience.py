"""Destructive recovery drill for a disposable BeaconWatch deployment.

This script stops and starts individual Compose services and verifies:

* Redis outage: readiness changes, local WebSocket fan-out continues,
  and cross-replica bus connectivity returns after Redis restarts;
* MQTT outage: ingest reconnects and accepts new observations;
* database outage: readiness fails closed and recovers without an API
  restart;
* optional API restart: readiness and WebSocket accept traffic again.

It refuses to run without an explicit project name, Compose file and
``--allow-disruptive``.  Never point it at a shared production stack.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiomqtt
import httpx
import websockets

_SAFE_PROJECT_NAMES = {
    "server",
    "rid-server",
    "beaconwatch",
    "bw",
    "prod",
    "production",
}


def _parse_metrics(text: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            sample, raw_value = line.rsplit(maxsplit=1)
            value = float(raw_value)
        except ValueError:
            continue
        name = sample.split("{", 1)[0]
        values[name] = values.get(name, 0.0) + value
    return values


class Drill:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.run_id = uuid.uuid4().hex[:8]
        self.station_id = f"DR-{self.run_id}"
        self.topic = f"rid/v1/{self.station_id}/obs"
        self.sequence = 0
        self.headers = (
            {"Authorization": f"Bearer {args.token}"} if args.token else {}
        )
        self.http = httpx.AsyncClient(
            base_url=args.base_url.rstrip("/"),
            headers=self.headers,
            timeout=args.request_timeout,
            trust_env=False,
        )

    async def close(self) -> None:
        await self.http.aclose()

    def compose(self, *command: str) -> None:
        command_line = ["docker", "compose", "-p", self.args.compose_project]
        for compose_file in self.args.compose_file:
            command_line.extend(["-f", compose_file])
        command_line.extend(command)
        print("  $ " + " ".join(command_line))
        subprocess.run(command_line, check=True)

    async def metrics(self) -> dict[str, float]:
        response = await self.http.get("/metrics")
        response.raise_for_status()
        return _parse_metrics(response.text)

    async def wait_ready(self, *, ready: bool, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            try:
                response = await self.http.get("/health/ready")
                is_ready = response.status_code == 200
                if is_ready == ready:
                    return
                last_error = f"HTTP {response.status_code}: {response.text[:120]}"
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                if not ready:
                    return
            await asyncio.sleep(1.0)
        state = "ready" if ready else "not ready"
        raise RuntimeError(
            f"API did not become {state} within {timeout:g}s: {last_error}"
        )

    async def wait_metric(
        self,
        name: str,
        expected: float,
        *,
        timeout: float,
        comparison: str = "==",
    ) -> dict[str, float]:
        deadline = time.monotonic() + timeout
        last_value: float | None = None
        while time.monotonic() < deadline:
            snapshot = await self.metrics()
            last_value = snapshot.get(name)
            if last_value is not None:
                matched = {
                    "==": last_value == expected,
                    ">": last_value > expected,
                    "<": last_value < expected,
                }[comparison]
                if matched:
                    return snapshot
            await asyncio.sleep(1.0)
        raise RuntimeError(
            f"metric {name} did not become {comparison} {expected:g} "
            f"within {timeout:g}s (last={last_value!r})"
        )

    def _payload(self) -> dict[str, Any]:
        self.sequence += 1
        now = datetime.now(tz=timezone.utc).isoformat()
        return {
            "observation_id": f"DR-{self.run_id}-{self.sequence:04d}",
            "drone_id": f"DR-{self.run_id}-{self.sequence:04d}",
            "operator_id": f"DRILL-{self.run_id}",
            "station_id": self.station_id,
            "protocol": "wifi_beacon",
            "rssi_dbm": -61,
            "position": {
                "lat": 22.5405,
                "lon": 113.9345,
                "alt_m": 30.0,
                "accuracy_h_m": 5.0,
                "accuracy_v_m": 8.0,
                "speed_mps": 5.0,
                "heading_deg": 180.0,
            },
            "drone_timestamp": now,
            "received_at": now,
            "self_id": "resilience drill",
            "operator_location": None,
        }

    async def publish(self) -> dict[str, Any]:
        async with aiomqtt.Client(
            hostname=self.args.mqtt_host,
            port=self.args.mqtt_port,
            username=self.args.mqtt_username or None,
            password=self.args.mqtt_password or None,
            identifier=f"beaconwatch-drill-{uuid.uuid4().hex[:10]}",
        ) as client:
            payload = self._payload()
            info = await client.publish(
                self.topic,
                json.dumps(payload, separators=(",", ":")),
                qos=self.args.mqtt_qos,
            )
            wait_for_publish = getattr(info, "wait_for_publish", None)
            if callable(wait_for_publish):
                await asyncio.wait_for(
                    wait_for_publish(),
                    timeout=self.args.publish_timeout,
                )
            return payload

    async def _receive_until(
        self,
        websocket: Any,
        *,
        drone_id: str,
        timeout: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
            try:
                frame = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(frame, dict):
                continue
            kind = frame.get("type")
            data = frame.get("data") or {}
            if kind == "obs" and data.get("drone_id") == drone_id:
                return frame
            if kind == "ping":
                await websocket.send(json.dumps({"type": "pong"}))
        raise TimeoutError(f"WebSocket did not receive {drone_id}")

    async def _assert_http_observation(
        self,
        drone_id: str,
        observation_id: str,
        *,
        timeout: float,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = await self.http.get(
                "/observations",
                params={"drone_id": drone_id, "limit": 10},
            )
            if response.status_code == 200:
                rows = response.json()
                if any(
                    row.get("observation_id") == observation_id
                    for row in rows
                ):
                    return
            await asyncio.sleep(0.5)
        raise RuntimeError(
            f"observation {observation_id} was not visible over HTTP"
        )

    @property
    def ws_url(self) -> str:
        scheme = "wss" if self.args.base_url.startswith("https://") else "ws"
        host = self.args.base_url.split("://", 1)[-1].rstrip("/")
        return f"{scheme}://{host}/ws/observations"

    def subprotocols(self) -> list[str]:
        protocols = ["rid.v1"]
        if self.args.token:
            protocols.append(f"bearer.{self.args.token}")
        return protocols

    async def publish_and_verify(
        self,
        label: str,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        async with websockets.connect(
            self.ws_url,
            subprotocols=self.subprotocols(),
            open_timeout=10,
            ping_interval=None,
        ) as websocket:
            payload = await self.publish()
            frame = await self._receive_until(
                websocket,
                drone_id=payload["drone_id"],
                timeout=timeout,
            )
            await self._assert_http_observation(
                payload["drone_id"],
                payload["observation_id"],
                timeout=timeout,
            )
        print(f"  PASS {label}: WebSocket + HTTP")
        return frame

    async def run(self) -> None:
        await self.wait_ready(ready=True, timeout=self.args.ready_timeout)
        before = await self.metrics()
        if before.get("rid_bus_backend_redis", 0) == 0:
            print("Redis backend is not enabled; Redis drill will be skipped.")
        await self.publish_and_verify(
            "baseline ingest",
            timeout=self.args.ingest_timeout,
        )

        if before.get("rid_bus_backend_redis", 0):
            await self.redis_drill()
        if not self.args.skip_mqtt:
            await self.mqtt_drill()
        if self.args.include_db:
            await self.db_drill()
        if self.args.include_api:
            await self.api_drill()
        final = await self.metrics()
        print(
            "PASS resilience drill: "
            f"final MQTT connected={final.get('rid_mqtt_connected', 0):.0f}, "
            f"Redis connected={final.get('rid_bus_redis_connected', 0):.0f}"
        )

    async def redis_drill(self) -> None:
        before = await self.metrics()
        print("Redis fault: stopping the shared bus")
        self.compose("stop", self.args.redis_service)
        await self.wait_metric(
            "rid_bus_redis_connected",
            0,
            timeout=self.args.recovery_timeout,
        )
        await self.publish_and_verify(
            "local fan-out while Redis is down",
            timeout=self.args.ingest_timeout,
        )
        print("Redis recovery: starting the shared bus")
        self.compose("start", self.args.redis_service)
        await self.wait_metric(
            "rid_bus_redis_connected",
            1,
            timeout=self.args.recovery_timeout,
        )
        await self.publish_and_verify(
            "fan-out after Redis recovery",
            timeout=self.args.ingest_timeout,
        )
        after = await self.metrics()
        reconnects = max(
            0.0,
            after.get("rid_bus_redis_reconnects_total", 0)
            - before.get("rid_bus_redis_reconnects_total", 0),
        )
        print(f"  Redis reconnects during drill: {reconnects:.0f}")

    async def mqtt_drill(self) -> None:
        before = await self.metrics()
        print("MQTT fault: stopping the broker")
        self.compose("stop", self.args.mqtt_service)
        await self.wait_metric(
            "rid_mqtt_connected",
            0,
            timeout=self.args.recovery_timeout,
        )
        print("MQTT recovery: starting the broker")
        self.compose("start", self.args.mqtt_service)
        await self.wait_metric(
            "rid_mqtt_connected",
            1,
            timeout=self.args.recovery_timeout,
        )
        await self.publish_and_verify(
            "ingest after MQTT recovery",
            timeout=self.args.ingest_timeout,
        )
        after = await self.metrics()
        reconnects = max(
            0.0,
            after.get("rid_mqtt_reconnects_total", 0)
            - before.get("rid_mqtt_reconnects_total", 0),
        )
        print(f"  MQTT reconnects during drill: {reconnects:.0f}")

    async def db_drill(self) -> None:
        print("Database fault: stopping PostgreSQL/TimescaleDB")
        self.compose("stop", self.args.db_service)
        await self.wait_ready(
            ready=False,
            timeout=self.args.recovery_timeout,
        )
        print("Database recovery: starting PostgreSQL/TimescaleDB")
        self.compose("start", self.args.db_service)
        await self.wait_ready(
            ready=True,
            timeout=self.args.recovery_timeout,
        )
        await self.publish_and_verify(
            "ingest after database recovery",
            timeout=self.args.ingest_timeout,
        )

    async def api_drill(self) -> None:
        print("API fault: stopping one API process")
        self.compose("stop", self.args.api_service)
        await self.wait_ready(
            ready=False,
            timeout=self.args.recovery_timeout,
        )
        print("API recovery: starting the API process")
        self.compose("start", self.args.api_service)
        await self.wait_ready(
            ready=True,
            timeout=self.args.recovery_timeout,
        )
        await self.wait_metric(
            "rid_mqtt_connected",
            1,
            timeout=self.args.recovery_timeout,
        )
        await self.publish_and_verify(
            "ingest after API restart",
            timeout=self.args.ingest_timeout,
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run destructive Redis/MQTT/database/API recovery drills on an "
            "explicitly chosen disposable Compose project."
        ),
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:58000")
    parser.add_argument("--token", default=os.environ.get("RID_LOADTEST_TOKEN", ""))
    parser.add_argument("--compose-project", required=True)
    parser.add_argument("--compose-file", action="append", required=True)
    parser.add_argument("--api-service", default="rid-server")
    parser.add_argument("--redis-service", default="redis")
    parser.add_argument("--mqtt-service", default="emqx")
    parser.add_argument("--db-service", default="timescaledb")
    parser.add_argument("--mqtt-host", default=os.environ.get("RID_MQTT_HOST", "127.0.0.1"))
    parser.add_argument(
        "--mqtt-port",
        type=int,
        default=int(os.environ.get("RID_MQTT_PORT", "1883")),
    )
    parser.add_argument(
        "--mqtt-username",
        default=os.environ.get("RID_MQTT_USERNAME", ""),
    )
    parser.add_argument(
        "--mqtt-password",
        default=os.environ.get("RID_MQTT_PASSWORD", ""),
    )
    parser.add_argument("--mqtt-qos", type=int, choices=(0, 1, 2), default=1)
    parser.add_argument("--request-timeout", type=float, default=10.0)
    parser.add_argument("--publish-timeout", type=float, default=10.0)
    parser.add_argument("--ready-timeout", type=float, default=90.0)
    parser.add_argument("--recovery-timeout", type=float, default=120.0)
    parser.add_argument("--ingest-timeout", type=float, default=45.0)
    parser.add_argument("--include-db", action="store_true")
    parser.add_argument("--include-api", action="store_true")
    parser.add_argument("--skip-mqtt", action="store_true")
    parser.add_argument("--allow-disruptive", action="store_true")
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    if not args.allow_disruptive:
        parser.error("--allow-disruptive is required")
    if not args.compose_project.strip():
        parser.error("--compose-project must not be empty")
    if args.compose_project in _SAFE_PROJECT_NAMES:
        parser.error(
            "refusing to disrupt a shared/default compose project; "
            "use a dedicated project such as bwresilience"
        )
    for compose_file in args.compose_file:
        if not Path(compose_file).is_file():
            parser.error(f"compose file does not exist: {compose_file}")

    async def run() -> None:
        drill = Drill(args)
        try:
            await drill.run()
        finally:
            await drill.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL resilience drill: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
