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

from rid_edge.common.config import get_settings
from rid_edge.common.dedup import Deduper
from rid_edge.common.logger import configure_logging, get_logger
from rid_edge.mqtt_publisher import MQTTPublisher
from rid_edge.pipeline.parser import observations_from_frame
from rid_edge.receivers import BLEReceiver, Receiver, WifiReceiver

log = get_logger(__name__)


async def _run_receiver(
    name: str,
    rx: Receiver,
    queue: asyncio.Queue,
) -> None:
    """Consume a receiver and push RawFrames into a shared queue."""
    await rx.start()
    try:
        async for frame in rx.frames():
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                log.warning("frame_queue_full", receiver=name)
    except Exception as exc:
        log.exception("receiver_crashed", receiver=name, error=str(exc))
    finally:
        await rx.stop()


async def _run_parser(
    station_id: str,
    in_queue: asyncio.Queue,
    out_queue: asyncio.Queue,
    dedup: Deduper,
) -> None:
    """Parse RawFrames into RidObservations and apply dedup."""
    while True:
        frame = await in_queue.get()
        try:
            for obs in observations_from_frame(frame):
                # Stamp station id
                obs = obs.model_copy(update={"station_id": station_id})
                # Dedup: only forward if it's the first within window
                is_dup = dedup.seen(
                    (obs.station_id, obs.drone_id, obs.protocol.value),
                    obs.observation_id,
                )
                if is_dup:
                    continue
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
            # publish to topic_info using the same outbox as obs
            # (we use the publisher's internal outbox by calling publish_observation?)
            # For simplicity we just use the paho client directly.
            if publisher._connected:
                publisher._client.publish(settings.topic_info, payload, qos=1)
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
    dedup = Deduper(window_s=settings.dedup_window_s, max_keys=settings.dedup_max_keys)

    # ---- receivers -----------------------------------------------
    receivers: list[tuple[str, Receiver]] = []
    if settings.wifi_enabled:
        receivers.append((
            "wifi",
            WifiReceiver(
                interface=settings.wifi_interface,
                channel_hop=settings.wifi_channel_hop,
                fixed_channel=settings.wifi_fixed_channel,
                rssi_min_dbm=settings.wifi_rssi_min_dbm,
            ),
        ))
    if settings.ble_enabled:
        receivers.append((
            "ble",
            BLEReceiver(
                scan_interval_s=settings.ble_scan_interval_s,
                scan_window_s=settings.ble_scan_window_s,
                rssi_min_dbm=settings.ble_rssi_min_dbm,
            ),
        ))

    publisher = MQTTPublisher(settings)
    await publisher.start()

    tasks = [
        asyncio.create_task(_run_heartbeat(publisher, settings), name="heartbeat"),
        asyncio.create_task(_run_parser(settings.station_id, frame_queue, obs_queue, dedup), name="parser"),
        asyncio.create_task(_run_publisher(publisher, obs_queue), name="publisher"),
    ]
    for name, rx in receivers:
        tasks.append(asyncio.create_task(_run_receiver(name, rx, frame_queue), name=f"rx:{name}"))

    # ---- signal handling ----------------------------------------
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    log.info("running", task_count=len(tasks))
    await stop.wait()
    log.info("shutting_down")

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
