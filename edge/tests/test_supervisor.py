"""Receiver supervision tests — no radio or broker required."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from rid_edge.receivers.base import Bearer, RawFrame, Receiver
from rid_edge.supervisor import ReceiverSupervisor


class ScriptedReceiver(Receiver):
    def __init__(self, control: dict[str, Any]) -> None:
        self._control = control

    async def start(self) -> None:
        self._control["starts"] += 1
        if self._control["starts"] <= self._control["fail_first"]:
            raise RuntimeError("radio disappeared")

    async def stop(self) -> None:
        self._control["stops"] += 1

    async def frames(self):
        yield self._control["frame"]
        await self._control["hang"].wait()


async def test_supervisor_restarts_crashed_receiver_without_losing_frames():
    control: dict[str, Any] = {
        "starts": 0,
        "stops": 0,
        "fail_first": 2,
        "hang": asyncio.Event(),
        "frame": RawFrame(
            bearer=Bearer.WIFI_NAN,
            source_mac="aa:bb:cc:dd:ee:ff",
            rssi_dbm=-61,
            payload=b"",
            timestamp=datetime.now(timezone.utc),
        ),
    }
    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    supervisor = ReceiverSupervisor(
        "wifi",
        lambda: ScriptedReceiver(control),
        queue,
        stop,
        base_backoff_s=0.001,
        max_backoff_s=0.01,
    )

    task = asyncio.create_task(supervisor.run())
    try:
        frame = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert frame is control["frame"]
        assert control["starts"] == 3
        assert supervisor.restarts == 2
        assert supervisor.last_error == "radio disappeared"
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert control["stops"] >= 3


async def test_supervisor_exits_promptly_when_stop_is_set():
    control: dict[str, Any] = {
        "starts": 0,
        "stops": 0,
        "fail_first": 1_000,
        "hang": asyncio.Event(),
        "frame": RawFrame(
            bearer=Bearer.BLE,
            source_mac="11:22:33:44:55:66",
            rssi_dbm=-70,
            payload=b"",
        ),
    }
    stop = asyncio.Event()
    supervisor = ReceiverSupervisor(
        "ble",
        lambda: ScriptedReceiver(control),
        asyncio.Queue(),
        stop,
        base_backoff_s=0.01,
        max_backoff_s=0.02,
    )
    task = asyncio.create_task(supervisor.run())
    await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert supervisor.restarts >= 1
