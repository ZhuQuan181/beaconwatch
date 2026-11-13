"""Pub/sub bus for the WebSocket fan-out.

Single-producer-multiple-consumer pattern: the MQTT subscriber pushes
newly-persisted observations to a single :class:`ObservationBus`; the
WebSocket endpoint has one :class:`asyncio.Queue` per connected client
and pulls from the bus.

This lives in memory only — when the server restarts, in-flight
WebSocket clients just reconnect.  (For cross-process fan-out, swap
this for a Redis pub/sub or NATS subject.  The interface here is
deliberately small to make that swap trivial.)
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from .models import DroneObservation


class ObservationBus:
    """In-process broadcast bus for live observations.

    The fan-out is a single :class:`asyncio.Condition` plus a list of
    subscriber queues.  Slow consumers are *dropped* (oldest item
    removed) so a single stuck client cannot backpressure the writer.
    """

    def __init__(self, max_queue_per_client: int = 256) -> None:
        self._subscribers: list[asyncio.Queue[DroneObservation]] = []
        self._max_queue = max_queue_per_client
        self._lock = asyncio.Lock()

    async def publish(self, obs: DroneObservation) -> None:
        async with self._lock:
            dead: list[asyncio.Queue[DroneObservation]] = []
            for q in self._subscribers:
                try:
                    q.put_nowait(obs)
                except asyncio.QueueFull:
                    # Only the slowest queue pays the eviction price.
                    # Drop ONE oldest item and retry.  If still full
                    # (which shouldn't happen with our maxsize=256 +
                    # single-eviction policy), drop the queue to
                    # avoid head-of-line blocking the publisher.
                    with _swallow():
                        q.get_nowait()
                    try:
                        q.put_nowait(obs)
                    except asyncio.QueueFull:  # pragma: no cover
                        dead.append(q)
            for q in dead:
                self._subscribers.remove(q)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[DroneObservation]]:
        q: asyncio.Queue[DroneObservation] = asyncio.Queue(
            maxsize=self._max_queue
        )
        async with self._lock:
            self._subscribers.append(q)
        try:
            yield q
        finally:
            async with self._lock:
                if q in self._subscribers:
                    self._subscribers.remove(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


class _swallow:
    """Tiny context manager that swallows :class:`asyncio.QueueEmpty`."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        return exc_type is asyncio.QueueEmpty
