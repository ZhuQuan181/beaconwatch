"""Repeatable BeaconWatch load test.

The script exercises the real HTTP, WebSocket and MQTT paths against a
disposable deployment.  It deliberately does not claim a production
capacity: the output records host information and server-side counters
so a baseline can be compared on the same machine and dataset.

Example:

    python scripts/load_test.py \
        --base-url http://127.0.0.1:58000 \
        --mqtt-host 127.0.0.1 --mqtt-port 51883 \
        --token "$RID_LOADTEST_TOKEN" \
        --duration 30 --http-concurrency 8 \
        --ws-clients 4 --ingest-rate 40 \
        --output-json /tmp/beaconwatch-load.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiomqtt
import httpx
import websockets


def _percentile(samples: list[float], percentile: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = min(
        len(ordered) - 1,
        max(0, round((percentile / 100.0) * (len(ordered) - 1))),
    )
    return ordered[index]


def _latency_summary(samples: list[float]) -> dict[str, float | int]:
    if not samples:
        return {
            "count": 0,
            "mean_ms": 0.0,
            "p50_ms": 0.0,
            "p95_ms": 0.0,
            "p99_ms": 0.0,
            "max_ms": 0.0,
        }
    milliseconds = [sample * 1000.0 for sample in samples]
    return {
        "count": len(milliseconds),
        "mean_ms": round(sum(milliseconds) / len(milliseconds), 3),
        "p50_ms": round(_percentile(milliseconds, 50), 3),
        "p95_ms": round(_percentile(milliseconds, 95), 3),
        "p99_ms": round(_percentile(milliseconds, 99), 3),
        "max_ms": round(max(milliseconds), 3),
    }


def _memory_total_mib() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        return None
    return None


def _system_info() -> dict[str, Any]:
    try:
        load_average = list(os.getloadavg())
    except OSError:
        load_average = []
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "memory_total_mib": _memory_total_mib(),
        "python": platform.python_version(),
        "load_average": load_average,
    }


def _parse_prometheus(text: str) -> dict[str, float]:
    """Return unlabeled sample values, summing labeled series by family."""

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


async def _fetch_metrics(
    client: httpx.AsyncClient,
) -> dict[str, float]:
    response = await client.get("/metrics")
    response.raise_for_status()
    return _parse_prometheus(response.text)


async def _drain_ingest(
    client: httpx.AsyncClient,
    *,
    before: dict[str, float],
    target_persisted: int,
    timeout_s: float,
) -> tuple[dict[str, float], float]:
    """Wait for the broker consumer to commit every published observation.

    Publishing with QoS 1 only proves that EMQX accepted the message.
    The server may still be draining its subscription while the publisher
    stops, so sample the durable counter until it catches up or the
    bounded drain window expires.
    """
    started = time.perf_counter()
    metrics = before
    if target_persisted <= 0 or timeout_s <= 0:
        return metrics, 0.0
    while True:
        metrics = await _fetch_metrics(client)
        persisted = _counter_delta(
            before, metrics, "rid_mqtt_persisted_total"
        )
        if persisted >= target_persisted:
            break
        remaining = timeout_s - (time.perf_counter() - started)
        if remaining <= 0:
            break
        await asyncio.sleep(min(0.25, remaining))
    return metrics, time.perf_counter() - started


def _counter_delta(
    before: dict[str, float],
    after: dict[str, float],
    name: str,
) -> float:
    return max(0.0, after.get(name, 0.0) - before.get(name, 0.0))


def _observation_payload(
    *,
    run_id: str,
    sequence: int,
    drone_count: int,
    station_id: str,
) -> dict[str, Any]:
    now = datetime.now(tz=timezone.utc).isoformat()
    drone_number = sequence % max(1, drone_count)
    return {
        "observation_id": f"LT-{run_id}-{sequence:09d}",
        "drone_id": f"LT-{run_id}-{drone_number:03d}",
        "operator_id": f"LOADTEST-{run_id}",
        "station_id": station_id,
        "protocol": "wifi_beacon",
        "rssi_dbm": -60,
        "position": {
            "lat": 22.5405 + (drone_number % 10) * 0.00001,
            "lon": 113.9345 + (drone_number % 10) * 0.00001,
            "alt_m": 30.0,
            "accuracy_h_m": 5.0,
            "accuracy_v_m": 8.0,
            "speed_mps": 5.0,
            "heading_deg": 180.0,
        },
        "drone_timestamp": now,
        "received_at": now,
        "self_id": "BeaconWatch load test",
        "operator_location": None,
    }


async def _http_worker(
    *,
    client: httpx.AsyncClient,
    url: str,
    stop: asyncio.Event,
    latencies: list[float],
    errors: list[str],
) -> None:
    while not stop.is_set():
        started = time.perf_counter()
        try:
            response = await client.get(url)
            response.raise_for_status()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if len(errors) < 20:
                errors.append(f"http:{type(exc).__name__}:{str(exc)[:120]}")
            await asyncio.sleep(0.05)
            continue
        latencies.append(time.perf_counter() - started)


async def _publisher(
    args: argparse.Namespace,
    *,
    topic: str,
    run_id: str,
    stop: asyncio.Event,
    sent: list[float],
    errors: list[str],
) -> None:
    if args.ingest_rate <= 0:
        return
    interval = 1.0 / args.ingest_rate
    sequence = 0
    async with aiomqtt.Client(
        hostname=args.mqtt_host,
        port=args.mqtt_port,
        username=args.mqtt_username or None,
        password=args.mqtt_password or None,
        identifier=f"beaconwatch-load-{uuid.uuid4().hex[:10]}",
    ) as client:
        while not stop.is_set():
            payload = json.dumps(
                _observation_payload(
                    run_id=run_id,
                    sequence=sequence,
                    drone_count=args.drone_count,
                    station_id=args.station_id,
                ),
                separators=(",", ":"),
            ).encode()
            started = time.perf_counter()
            try:
                info = await client.publish(
                    topic,
                    payload,
                    qos=args.mqtt_qos,
                )
                wait_for_publish = getattr(info, "wait_for_publish", None)
                if callable(wait_for_publish):
                    await asyncio.wait_for(
                        wait_for_publish(),
                        timeout=args.publish_timeout,
                    )
                sent.append(time.perf_counter() - started)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if len(errors) < 20:
                    errors.append(
                        f"mqtt:{type(exc).__name__}:{str(exc)[:120]}"
                    )
                await asyncio.sleep(0.1)
            sequence += 1
            await asyncio.sleep(interval)


async def _websocket_worker(
    *,
    url: str,
    subprotocols: list[str],
    stop: asyncio.Event,
    run_id: str,
    stats: dict[str, Any],
    errors: list[str],
) -> None:
    try:
        async with websockets.connect(
            url,
            subprotocols=subprotocols or None,
            open_timeout=10,
            close_timeout=2,
            ping_interval=None,
        ) as websocket:
            stats["connected"] += 1
            idle_deadline = time.monotonic() + 60.0
            while not stop.is_set():
                timeout = max(0.05, min(1.0, idle_deadline - time.monotonic()))
                try:
                    raw = await asyncio.wait_for(
                        websocket.recv(),
                        timeout=timeout,
                    )
                except TimeoutError:
                    if time.monotonic() >= idle_deadline:
                        errors.append("ws:no-frame-for-60s")
                        stats["disconnects"] += 1
                        return
                    continue
                stats["frames"] += 1
                idle_deadline = time.monotonic() + 60.0
                try:
                    frame = json.loads(raw)
                except (TypeError, ValueError):
                    stats["malformed"] += 1
                    continue
                kind = frame.get("type") if isinstance(frame, dict) else None
                if kind == "ping":
                    stats["pings"] += 1
                elif kind == "pong":
                    stats["pongs"] += 1
                elif kind in {"obs", "alert"}:
                    stats["data_frames"] += 1
                    data = frame.get("data") or {}
                    if str(data.get("drone_id", "")).startswith(f"LT-{run_id}-"):
                        stats["load_observations"] += 1
                    if kind == "alert":
                        stats["alerts"] += 1
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        stats["disconnects"] += 1
        if len(errors) < 20:
            errors.append(f"ws:{type(exc).__name__}:{str(exc)[:120]}")
    else:
        if not stop.is_set():
            stats["disconnects"] += 1
            if len(errors) < 20:
                errors.append("ws:closed-early")


async def run(args: argparse.Namespace) -> int:
    run_id = uuid.uuid4().hex[:8]
    args.station_id = args.station_id or f"LT-{run_id}"
    topic = f"rid/v1/{args.station_id}/obs"
    headers = (
        {"Authorization": f"Bearer {args.token}"} if args.token else {}
    )
    http_errors: list[str] = []
    mqtt_errors: list[str] = []
    ws_errors: list[str] = []
    http_latencies: list[float] = []
    publish_latencies: list[float] = []
    ws_stats: dict[str, Any] = {
        "connected": 0,
        "disconnects": 0,
        "frames": 0,
        "data_frames": 0,
        "load_observations": 0,
        "alerts": 0,
        "pings": 0,
        "pongs": 0,
        "malformed": 0,
    }

    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        headers=headers,
        timeout=args.request_timeout,
        trust_env=False,
    ) as client:
        ready = await client.get("/health/ready")
        ready.raise_for_status()
        if args.warmup_s:
            print(f"warming up for {args.warmup_s:g}s ...")
            await asyncio.sleep(args.warmup_s)
        before = await _fetch_metrics(client)

        stop = asyncio.Event()
        ws_stop = asyncio.Event()
        subprotocols = ["rid.v1"]
        if args.token:
            subprotocols.append(f"bearer.{args.token}")
        ws_tasks = [
            asyncio.create_task(
                _websocket_worker(
                    url=args.ws_url,
                    subprotocols=subprotocols,
                    stop=ws_stop,
                    run_id=run_id,
                    stats=ws_stats,
                    errors=ws_errors,
                ),
                name=f"load-ws-{index}",
            )
            for index in range(args.ws_clients)
        ]
        ws_deadline = time.monotonic() + args.ws_warmup_s
        while (
            ws_stats["connected"] < args.ws_clients
            and time.monotonic() < ws_deadline
        ):
            await asyncio.sleep(0.05)
        if ws_stats["connected"] < args.ws_clients and ws_errors:
            print(f"WebSocket warm-up errors: {ws_errors[:3]}")

        http_tasks = [
            asyncio.create_task(
                _http_worker(
                    client=client,
                    url=args.http_path,
                    stop=stop,
                    latencies=http_latencies,
                    errors=http_errors,
                ),
                name=f"load-http-{index}",
            )
            for index in range(args.http_concurrency)
        ]
        publisher_task = asyncio.create_task(
            _publisher(
                args,
                topic=topic,
                run_id=run_id,
                stop=stop,
                sent=publish_latencies,
                errors=mqtt_errors,
            ),
            name="load-mqtt-publisher",
        )

        started_at = datetime.now(tz=timezone.utc)
        started = time.perf_counter()
        print(
            f"load started: duration={args.duration:g}s "
            f"http_concurrency={args.http_concurrency} "
            f"ws_clients={args.ws_clients} ingest_rate={args.ingest_rate:g}/s"
        )
        await asyncio.sleep(args.duration)
        stop.set()
        await asyncio.gather(*http_tasks, return_exceptions=True)
        await publisher_task
        load_elapsed = max(0.001, time.perf_counter() - started)
        after, drain_elapsed = await _drain_ingest(
            client,
            before=before,
            target_persisted=len(publish_latencies),
            timeout_s=args.drain_s,
        )
        ws_stop.set()
        await asyncio.gather(*ws_tasks, return_exceptions=True)
        elapsed = max(0.001, time.perf_counter() - started)
        after = await _fetch_metrics(client)

    server_delta = {
        "mqtt_received": _counter_delta(
            before, after, "rid_mqtt_received_total"
        ),
        "mqtt_persisted": _counter_delta(
            before, after, "rid_mqtt_persisted_total"
        ),
        "mqtt_duplicates": _counter_delta(
            before, after, "rid_mqtt_duplicates_total"
        ),
        "mqtt_invalid": _counter_delta(
            before, after, "rid_mqtt_invalid_total"
        ),
        "mqtt_reconnects": _counter_delta(
            before, after, "rid_mqtt_reconnects_total"
        ),
        "ws_delivered": _counter_delta(
            before, after, "rid_ws_delivered_frames_total"
        ),
        "ws_dropped": _counter_delta(
            before, after, "rid_ws_dropped_frames_total"
        ),
        "redis_received": _counter_delta(
            before, after, "rid_bus_redis_received_frames_total"
        ),
        "redis_publish_failures": _counter_delta(
            before, after, "rid_bus_redis_publish_failures_total"
        ),
    }
    http_error_rate = (
        len(http_errors) / max(1, len(http_latencies) + len(http_errors))
    )
    publish_error_rate = (
        len(mqtt_errors)
        / max(1, len(publish_latencies) + len(mqtt_errors))
    )
    published = len(publish_latencies)
    persisted = int(server_delta["mqtt_persisted"])
    ingest_ratio = persisted / published if published else 1.0
    connected_ws_clients = min(ws_stats["connected"], args.ws_clients)
    expected_ws_observations = persisted * connected_ws_clients
    ws_delivery_ratio = (
        ws_stats["load_observations"] / expected_ws_observations
        if expected_ws_observations
        else 1.0
    )
    end_to_end_ws_observations = published * connected_ws_clients
    ws_end_to_end_ratio = (
        ws_stats["load_observations"] / end_to_end_ws_observations
        if end_to_end_ws_observations
        else 1.0
    )
    result = {
        "schema": 2,
        "label": args.label,
        "started_at": started_at.isoformat(),
        "elapsed_s": round(elapsed, 3),
        "load_elapsed_s": round(load_elapsed, 3),
        "drain_elapsed_s": round(drain_elapsed, 3),
        "system": _system_info(),
        "parameters": {
            "base_url": args.base_url,
            "http_path": args.http_path,
            "http_concurrency": args.http_concurrency,
            "ws_clients": args.ws_clients,
            "ingest_rate": args.ingest_rate,
            "duration_s": args.duration,
            "drain_s": args.drain_s,
            "ws_warmup_s": args.ws_warmup_s,
            "drone_count": args.drone_count,
            "station_id": args.station_id,
            "mqtt_qos": args.mqtt_qos,
        },
        "http": {
            "throughput_rps": round(len(http_latencies) / load_elapsed, 3),
            "error_rate": round(http_error_rate, 6),
            **_latency_summary(http_latencies),
            "errors": http_errors,
        },
        "ingest": {
            "published": published,
            "persisted": persisted,
            "persisted_ratio": round(ingest_ratio, 6),
            "persisted_throughput_rps": round(
                persisted / max(elapsed, 0.001), 3
            ),
            "publish_throughput_rps": round(
                published / load_elapsed, 3
            ),
            "publish_error_rate": round(publish_error_rate, 6),
            **_latency_summary(publish_latencies),
            "errors": mqtt_errors,
        },
        "websocket": {
            **ws_stats,
            "counted_clients": connected_ws_clients,
            "expected_observations": expected_ws_observations,
            "end_to_end_expected_observations": end_to_end_ws_observations,
            "delivery_ratio": round(ws_delivery_ratio, 6),
            "end_to_end_ratio": round(ws_end_to_end_ratio, 6),
            "errors": ws_errors,
        },
        "server_delta": server_delta,
    }

    print("\nBeaconWatch load test")
    print(
        f"  HTTP  {result['http']['throughput_rps']:.1f} req/s  "
        f"p50={result['http']['p50_ms']:.1f}ms  "
        f"p95={result['http']['p95_ms']:.1f}ms  "
        f"p99={result['http']['p99_ms']:.1f}ms  "
        f"errors={result['http']['error_rate']:.2%}"
    )
    print(
        f"  Ingest {result['ingest']['publish_throughput_rps']:.1f} msg/s  "
        f"persisted={persisted}  ratio={ingest_ratio:.1%}  "
        f"server={result['ingest']['persisted_throughput_rps']:.1f} msg/s  "
        f"duplicates={server_delta['mqtt_duplicates']:.0f}  "
        f"invalid={server_delta['mqtt_invalid']:.0f}  "
        f"drain={drain_elapsed:.1f}s"
    )
    print(
        f"  WS    connected={connected_ws_clients}/{args.ws_clients}  "
        f"disconnects={ws_stats['disconnects']}  "
        f"load_obs={ws_stats['load_observations']}  "
        f"persisted-delivery={ws_delivery_ratio:.1%}  "
        f"end-to-end={ws_end_to_end_ratio:.1%}  "
        f"server_dropped={server_delta['ws_dropped']:.0f}"
    )
    print(
        f"  Server MQTT reconnects={server_delta['mqtt_reconnects']:.0f}  "
        f"Redis publish failures={server_delta['redis_publish_failures']:.0f}"
    )

    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"  JSON  {output_path}")

    failed = (
        http_error_rate > args.max_error_rate
        or publish_error_rate > args.max_error_rate
        or bool(ws_errors)
        or ws_stats["disconnects"] > 0
        or connected_ws_clients < args.ws_clients
        or ws_delivery_ratio < args.min_ws_delivery_ratio
        or (
            args.ingest_rate > 0
            and persisted < published * args.min_ingest_ratio
        )
    )
    if failed:
        print("FAIL: one or more load-test thresholds were not met")
        return 1
    print("PASS: load-test thresholds met")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a reproducible BeaconWatch HTTP/MQTT/WS load test.",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--ws-url", default="")
    parser.add_argument("--token", default=os.environ.get("RID_LOADTEST_TOKEN", ""))
    parser.add_argument("--http-path", default="/observations?limit=50")
    parser.add_argument("--http-concurrency", type=int, default=8)
    parser.add_argument("--ws-clients", type=int, default=2)
    parser.add_argument("--ws-warmup-s", type=float, default=2.0)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--warmup-s", type=float, default=0.0)
    parser.add_argument("--drain-s", type=float, default=10.0)
    parser.add_argument("--request-timeout", type=float, default=10.0)
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
    parser.add_argument("--publish-timeout", type=float, default=5.0)
    parser.add_argument("--ingest-rate", type=float, default=20.0)
    parser.add_argument("--drone-count", type=int, default=8)
    parser.add_argument("--station-id", default="")
    parser.add_argument("--label", default="")
    parser.add_argument("--output-json", default="")
    parser.add_argument("--max-error-rate", type=float, default=0.01)
    parser.add_argument("--min-ws-delivery-ratio", type=float, default=0.95)
    parser.add_argument("--min-ingest-ratio", type=float, default=0.95)
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    if args.http_concurrency < 1:
        parser.error("--http-concurrency must be >= 1")
    if args.ws_clients < 0:
        parser.error("--ws-clients must be >= 0")
    if args.ws_warmup_s < 0:
        parser.error("--ws-warmup-s must be >= 0")
    if args.ingest_rate < 0:
        parser.error("--ingest-rate must be >= 0")
    if args.duration <= 0:
        parser.error("--duration must be > 0")
    if not args.ws_url:
        scheme = "wss" if args.base_url.startswith("https://") else "ws"
        host = args.base_url.split("://", 1)[-1].rstrip("/")
        args.ws_url = f"{scheme}://{host}/ws/observations"
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
