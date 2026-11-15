"""Live WebSocket fan-out with optional Redis cross-replica delivery.

The default bus is in-process.  When ``RID_BUS_BACKEND=redis`` the same
frames are published to a Redis channel and every API replica forwards
the received frame to its local WebSocket queues.  Redis Pub/Sub is
deliberately at-most-once: a client that is disconnected can recover
history through REST, while a failed Redis publish falls back to local
fan-out so ingest and the local dashboard keep working.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import redis.asyncio as redis_asyncio

from .config import Settings
from .logging_setup import get_logger
from .models import DroneObservation

log = get_logger(__name__)


class ObservationBus:
    """In-process broadcast bus for live observations and alerts.

    The fan-out is a single :class:`asyncio.Condition` plus a list of
    subscriber queues.  Slow consumers are *dropped* (oldest item
    removed) so a single stuck client cannot backpressure the writer.
    """

    def __init__(self, max_queue_per_client: int = 256) -> None:
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        self._max_queue = max_queue_per_client
        self._lock = asyncio.Lock()
        self._dropped_frames = 0
        self._delivered_frames = 0
        self._connected = True
        self._reconnects = 0
        self._publish_failures = 0
        self._remote_received = 0

    async def start(self) -> None:
        """Start a backend-specific listener; memory mode is a no-op."""

    async def stop(self) -> None:
        """Stop a backend-specific listener; memory mode is a no-op."""

    async def publish(self, obs: DroneObservation) -> None:
        await self._publish_frame(
            {"kind": "obs", "data": obs.model_dump(mode="json")}
        )

    async def publish_alert(self, alert) -> None:
        """Broadcast an alert.

        Accepts either an ``Alert`` dataclass or a plain dict, so the
        WS layer doesn't need to know which module produced it.
        """
        payload = alert if isinstance(alert, dict) else alert.__dict__
        await self._publish_frame({"kind": "alert", "data": payload})

    async def _publish_frame(self, frame: dict[str, Any]) -> None:
        await self._push(frame)

    async def _push(self, frame: dict[str, Any]) -> None:
        async with self._lock:
            dead: list[asyncio.Queue[dict[str, Any]]] = []
            for q in self._subscribers:
                try:
                    q.put_nowait(frame)
                    self._delivered_frames += 1
                except asyncio.QueueFull:
                    # Only the slowest queue pays the eviction price.
                    # Drop ONE oldest item and retry.  If still full
                    # (which shouldn't happen with our maxsize=256 +
                    # single-eviction policy), drop the queue to
                    # avoid head-of-line blocking the publisher.
                    with _swallow():
                        q.get_nowait()
                    self._dropped_frames += 1
                    try:
                        q.put_nowait(frame)
                        self._delivered_frames += 1
                    except asyncio.QueueFull:  # pragma: no cover
                        dead.append(q)
            for q in dead:
                self._subscribers.remove(q)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._max_queue)
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
        # ``len`` is atomic under CPython and reflects test/direct
        # registrations as well as the context-manager path.  A cached
        # counter could drift when the list is mutated outside ``_push``.
        return len(self._subscribers)

    @property
    def dropped_frames(self) -> int:
        return self._dropped_frames

    @property
    def delivered_frames(self) -> int:
        return self._delivered_frames

    @property
    def backend(self) -> str:
        return "memory"

    @property
    def redis_connected(self) -> bool:
        return False

    @property
    def reconnects(self) -> int:
        return self._reconnects

    @property
    def publish_failures(self) -> int:
        return self._publish_failures

    @property
    def remote_received_frames(self) -> int:
        return self._remote_received


class RedisObservationBus(ObservationBus):
    """Redis Pub/Sub bridge with local queue fan-out and reconnect logic."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: Any | None = None,
        channel: str | None = None,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._channel = channel or settings.redis_channel
        self._client = client or redis_asyncio.Redis.from_url(
            settings.redis_url,
            decode_responses=False,
            socket_connect_timeout=settings.redis_connect_timeout_s,
            # Pub/Sub is idle most of the time. A finite socket timeout
            # would tear down a healthy subscription after every quiet
            # interval. The listener uses get_message(timeout=...) for
            # bounded idle reads instead.
            socket_timeout=None,
            socket_keepalive=True,
            health_check_interval=15,
        )
        self._pubsub: Any | None = None
        self._listen_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._connected = False
        self._last_error: str | None = None

    async def start(self) -> None:
        if self._listen_task is not None:
            return
        self._stop.clear()
        try:
            await self._connect()
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)[:200]
            self._connected = False
            log.warning(
                "bus.redis.initial_connect_failed",
                error=self._last_error,
                required=self._settings.redis_required,
            )
            # The API stays up and retries in the background. When Redis
            # is required, /health/ready remains 503 until the listener
            # reconnects, which is easier to operate than a crash loop.
        self._listen_task = asyncio.create_task(
            self._listen_loop(),
            name="redis-observation-bus",
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._listen_task is not None:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "bus.redis.listener_stop_failed",
                    error=str(exc)[:200],
                )
            self._listen_task = None
        await self._close_pubsub()
        self._connected = False
        close = getattr(self._client, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception as exc:  # noqa: BLE001
                log.debug("bus.redis.close_failed", error=str(exc)[:200])

    async def _close_pubsub(self) -> None:
        pubsub = self._pubsub
        self._pubsub = None
        if pubsub is None:
            return
        close = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
        if close is None:
            return
        try:
            result = close()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001
            log.debug("bus.redis.pubsub_close_failed", error=str(exc)[:200])

    async def _connect(self) -> None:
        await self._close_pubsub()
        pubsub = self._client.pubsub(ignore_subscribe_messages=True)
        await asyncio.wait_for(
            pubsub.subscribe(self._channel),
            timeout=self._settings.redis_connect_timeout_s,
        )
        self._pubsub = pubsub
        self._connected = True
        self._last_error = None
        log.info("bus.redis.connected", channel=self._channel)

    async def _listen_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                if not self._connected or self._pubsub is None:
                    await self._connect()
                    backoff = 1.0
                pubsub = self._pubsub
                if pubsub is None:  # pragma: no cover - guarded by _connect
                    raise RuntimeError("Redis Pub/Sub is not connected")
                while (
                    not self._stop.is_set()
                    and self._connected
                    and self._pubsub is pubsub
                ):
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=self._settings.redis_poll_timeout_s,
                    )
                    if message is None:
                        continue
                    await self._handle_message(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._connected = False
                self._last_error = str(exc)[:200]
                self._reconnects += 1
                await self._close_pubsub()
                log.warning(
                    "bus.redis.disconnected",
                    error=self._last_error,
                    backoff=backoff,
                )
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=backoff,
                    )
                except asyncio.TimeoutError:
                    pass
                backoff = min(
                    backoff * 2.0,
                    self._settings.redis_reconnect_max_s,
                )

    async def _handle_message(self, message: object) -> None:
        if not isinstance(message, dict):
            return
        if message.get("type") != "message":
            return
        raw = message.get("data")
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                log.warning("bus.redis.invalid_utf8")
                return
        if not isinstance(raw, str):
            return
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("bus.redis.invalid_frame")
            return
        if not isinstance(frame, dict) or frame.get("kind") not in {
            "obs",
            "alert",
        }:
            log.warning("bus.redis.unknown_frame")
            return
        await self._push(frame)
        self._remote_received += 1

    async def _publish_frame(self, frame: dict[str, Any]) -> None:
        if not self._connected:
            self._publish_failures += 1
            await self._push(frame)
            return
        try:
            await asyncio.wait_for(
                self._client.publish(
                    self._channel,
                    json.dumps(frame, separators=(",", ":")).encode(),
                ),
                timeout=self._settings.redis_publish_timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._publish_failures += 1
            self._connected = False
            self._reconnects += 1
            self._last_error = str(exc)[:200]
            log.warning(
                "bus.redis.publish_failed",
                error=self._last_error,
            )
            # Do not lose the local dashboard when Redis is down.  The
            # remote replicas will recover when the listener reconnects.
            await self._push(frame)

    @property
    def backend(self) -> str:
        return "redis"

    @property
    def redis_connected(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> str | None:
        return self._last_error


def create_bus(settings: Settings) -> ObservationBus:
    """Create the configured bus without making Redis mandatory by default."""

    if settings.bus_backend == "redis":
        return RedisObservationBus(settings)
    return ObservationBus()


class _swallow:
    """Tiny context manager that swallows :class:`asyncio.QueueEmpty`."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        return exc_type is asyncio.QueueEmpty
