"""Restart and supervise a radio receiver without letting one crash stop the edge."""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

from rid_edge.common.backoff import compute_delay
from rid_edge.common.logger import get_logger
from rid_edge.receivers.base import Receiver, ReceiverError

log = get_logger(__name__)


class ReceiverSupervisor:
    """Keep a receiver alive with bounded exponential backoff.

    A receiver returns from ``frames()`` only when it has stopped; treat
    that as a crash unless the process is shutting down.  The receiver is
    recreated for every attempt so a half-initialised adapter cannot leak
    state into the next run.
    """

    def __init__(
        self,
        name: str,
        receiver_factory: Callable[[], Receiver],
        output_queue: asyncio.Queue,
        stop_event: asyncio.Event,
        *,
        base_backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
    ) -> None:
        self.name = name
        self._receiver_factory = receiver_factory
        self._output_queue = output_queue
        self._stop_event = stop_event
        self._base_backoff_s = base_backoff_s
        self._max_backoff_s = max_backoff_s
        self.restarts = 0
        self.dropped_frames = 0
        self.last_error: str | None = None

    async def run(self) -> None:
        attempt = 0
        while not self._stop_event.is_set():
            receiver = self._receiver_factory()
            try:
                await receiver.start()
                async for frame in receiver.frames():
                    if self._stop_event.is_set():
                        return
                    try:
                        self._output_queue.put_nowait(frame)
                    except asyncio.QueueFull:
                        self.dropped_frames += 1
                        log.warning(
                            "receiver_queue_full",
                            receiver=self.name,
                            dropped=self.dropped_frames,
                        )
                    else:
                        # A receiver that produced data is healthy; the next
                        # crash should retry quickly instead of inheriting the
                        # old exponential-backoff ceiling.
                        attempt = 0
                if self._stop_event.is_set():
                    return
                raise ReceiverError(f"{self.name} receiver stream ended")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                log.exception(
                    "receiver_crashed",
                    receiver=self.name,
                    error=str(exc),
                    restarts=self.restarts,
                )
                if self._stop_event.is_set():
                    return
                delay = compute_delay(
                    attempt,
                    base_s=self._base_backoff_s,
                    cap_s=self._max_backoff_s,
                )
                attempt += 1
                self.restarts += 1
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            finally:
                with contextlib.suppress(Exception):
                    await receiver.stop()

    @property
    def stats(self) -> dict[str, int | str | None]:
        return {
            "restarts": self.restarts,
            "dropped": self.dropped_frames,
            "last_error": self.last_error,
        }


__all__ = ["ReceiverSupervisor"]
