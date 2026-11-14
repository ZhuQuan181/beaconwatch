"""Edge station main entrypoint.

Wires up:
- Settings (env / .env)
- Logging
- Wi-Fi + BLE receivers (asyncio tasks)
- Parser pipeline
- MQTT publisher (with offline buffer)

Run with::

    RID_STATION_ID=station-01 python -m rid_edge.main
"""
from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from collections.abc import Callable

from rid_edge.common.config import get_settings
from rid_edge.common.logger import configure_logging, get_logger
from rid_edge.mqtt_publisher import MQTTPublisher
from rid_edge.pipeline import PipelineManager
from rid_edge.receivers import BLEReceiver, Receiver, WifiReceiver
from rid_edge.supervisor import ReceiverSupervisor

log = get_logger(__name__)


async def _run_parser(
    pipeline: PipelineManager,
    in_queue: asyncio.Queue,
    out_queue: asyncio.Queue,
) -> None:
    """Parse RawFrames through the shared dedup/rate-limited pipeline."""
    while True:
        frame = await in_queue.get()
        try:
            for obs in pipeline.process(frame):
                await out_queue.put(obs)
        except Exception as exc:
            log.exception("parser_error", error=str(exc))


async def _run_publisher(
    publisher: MQTTPublisher,
    in_queue: asyncio.Queue,
) -> None:
    while True:
        obs = await in_queue.get()
        await publisher.publish(obs)


async def _run_heartbeat(
    publisher: MQTTPublisher,
    settings,
) -> None:
    """Periodically publish station info so the server knows we're alive."""
    from rid_edge.models import Position, StationInfo

    info = StationInfo(
        station_id=settings.station_id,
        location=Position(
            lat=settings.station_lat,
            lon=settings.station_lon,
            alt_m=settings.station_alt_m,
        ),
        name=settings.station_name,
        firmware_version=settings.firmware_version,
    )
    while True:
        try:
            payload = info.model_dump_json().encode("utf-8")
            await publisher.publish_message(settings.topic_info, payload)
        except Exception as exc:
            log.warning("heartbeat_failed", error=str(exc))
        await asyncio.sleep(settings.info_interval_s)


async def main() -> int:
    configure_logging()
    settings = get_settings()
    log.info(
        "starting",
        station_id=settings.station_id,
        wifi=settings.wifi_enabled,
        ble=settings.ble_enabled,
        mqtt=f"{settings.mqtt_host}:{settings.mqtt_port}",
    )

    # ---- queues ---------------------------------------------------
    frame_queue: asyncio.Queue = asyncio.Queue(maxsize=4096)
    obs_queue: asyncio.Queue = asyncio.Queue(maxsize=4096)
    pipeline = PipelineManager(
        station_id=settings.station_id,
        dedup_window_s=settings.dedup_window_s,
        max_obs_per_sec=settings.max_obs_per_sec,
        dedup_max_keys=settings.dedup_max_keys,
    )

    # ---- receivers -----------------------------------------------
    receiver_specs: list[tuple[str, Callable[[], Receiver]]] = []
    if settings.wifi_enabled:
        receiver_specs.append((
            "wifi",
            lambda: WifiReceiver(
                interface=settings.wifi_interface,
                channel_hop=settings.wifi_channel_hop,
                fixed_channel=settings.wifi_fixed_channel,
                rssi_min_dbm=settings.wifi_rssi_min_dbm,
            ),
        ))
    if settings.ble_enabled:
        receiver_specs.append((
            "ble",
            lambda: BLEReceiver(
                scan_interval_s=settings.ble_scan_interval_s,
                scan_window_s=settings.ble_scan_window_s,
                rssi_min_dbm=settings.ble_rssi_min_dbm,
            ),
        ))

    publisher = MQTTPublisher(settings)
    await publisher.start()

    stop = asyncio.Event()
    supervisors: list[ReceiverSupervisor] = []
    tasks = [
        asyncio.create_task(_run_heartbeat(publisher, settings), name="heartbeat"),
        asyncio.create_task(
            _run_parser(pipeline, frame_queue, obs_queue),
            name="parser",
        ),
        asyncio.create_task(_run_publisher(publisher, obs_queue), name="publisher"),
    ]
    for name, factory in receiver_specs:
        supervisor = ReceiverSupervisor(
            name,
            factory,
            frame_queue,
            stop,
            base_backoff_s=settings.receiver_backoff_base_s,
            max_backoff_s=settings.receiver_backoff_max_s,
        )
        supervisors.append(supervisor)
        tasks.append(
            asyncio.create_task(supervisor.run(), name=f"rx:{name}")
        )

    # ---- signal handling ----------------------------------------
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    log.info("running", task_count=len(tasks))
    await stop.wait()
    log.info(
        "shutting_down",
        receiver_stats={supervisor.name: supervisor.stats for supervisor in supervisors},
    )

    for t in tasks:
        t.cancel()
    for t in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await t
    await publisher.stop()
    log.info("bye")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
