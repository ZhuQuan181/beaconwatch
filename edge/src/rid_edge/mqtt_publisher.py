"""MQTT publisher with TLS, offline buffering, and reconnection backoff.

Flow
----
1. Connect to broker (with TLS, optional client cert).
2. Maintain a persistent in-process queue of pending observations.
3. Background task: drain queue → publish → on failure buffer to
   disk-backed SQLite → reconnect with exponential backoff.
4. Drain the SQLite buffer on every successful (re)connect.

Why SQLite for the buffer?
- Atomic writes via WAL — even if the industrial PC loses power we
  don't lose more than the last in-flight row.
- No external dependencies (no Redis, no extra service).
- A separate process (``sqlite-wal-checkpoint``) can vacuum the file
  weekly from a cron job.

Wire format
-----------
Topic: ``rid/v1/{station_id}/obs`` — JSON payload of :class:`RidObservation`.
QoS 1 (at least once) so the server can dedup by ``observation_id``.
"""
from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time
from pathlib import Path
from typing import Optional

from paho.mqtt.client import Client as PahoClient

from .common.backoff import compute_delay
from .common.ulid import new_ulid
from .models import RidObservation


# ----------------------------------------------------------------------
# Offline buffer
# ----------------------------------------------------------------------
class _OfflineBuffer:
    """Tiny SQLite-backed queue.

    Each pending message is one row.  On successful publish we delete
    the row in the same transaction that adds any new ones, so we never
    have to deal with row-vs-payload state divergence.
    """

    __slots__ = ("_conn", "_path", "_max_bytes")

    def __init__(self, path: Path, max_bytes: int) -> None:
        self._path = path
        self._max_bytes = int(max_bytes)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._path), isolation_level=None, check_same_thread=False
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending (
                seq    INTEGER PRIMARY KEY AUTOINCREMENT,
                topic  TEXT    NOT NULL,
                qos    INTEGER NOT NULL,
                payload BLOB   NOT NULL,
                ts     REAL    NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS pending_ts_idx ON pending(seq)"
        )

    def push(self, topic: str, qos: int, payload: bytes) -> None:
        if len(topic.encode("utf-8")) + len(payload) > self._max_bytes:
            return  # drop oversized, not worth blocking on
        self._conn.execute(
            "INSERT INTO pending(topic, qos, payload, ts) VALUES (?, ?, ?, ?)",
            (topic, int(qos), payload, time.time()),
        )
        self._enforce_cap()

    def _enforce_cap(self) -> None:
        while True:
            size = self._pending_bytes()
            if size <= self._max_bytes:
                return
            count = int(
                self._conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
            )
            if count == 0:
                return
            # Evict the oldest quarter (at least one) to avoid churn.
            n = max(1, count // 4)
            self._conn.execute(
                "DELETE FROM pending WHERE seq IN ("
                "  SELECT seq FROM pending ORDER BY seq ASC LIMIT ?"
                ")",
                (n,),
            )

    def _pending_bytes(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(LENGTH(topic) + LENGTH(payload)), 0) "
            "FROM pending"
        ).fetchone()
        return int(row[0])

    def drain_batch(self, limit: int = 200) -> list[tuple[int, str, int, bytes]]:
        cur = self._conn.execute(
            "SELECT seq, topic, qos, payload FROM pending ORDER BY seq ASC LIMIT ?",
            (limit,),
        )
        return [(int(r[0]), r[1], int(r[2]), r[3]) for r in cur.fetchall()]

    def delete(self, seq: int) -> None:
        self._conn.execute("DELETE FROM pending WHERE seq = ?", (seq,))

    def size(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) FROM pending")
        return int(cur.fetchone()[0])

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.close()


# ----------------------------------------------------------------------
# Publisher
# ----------------------------------------------------------------------
class MQTTPublisher:
    """Async wrapper around paho-mqtt.

    Why paho and not aiomqtt?  paho has a battle-tested C-backed
    connection (mosquitto) that survives long outages without losing
    its TLS session.  aiomqtt is a thin async wrapper over paho; we
    use it in tests where we want to inject a mock broker, and we
    leave the option of switching by isolating the calls to two
    methods (``_publish`` and ``_loop_forever``).
    """

    __slots__ = (
        "_settings",
        "_client",
        "_buffer",
        "_outbox",
        "_publisher_task",
        "_loop_task",
        "_drain_task",
        "_loop",
        "_connected",
        "_dropped",
        "_sent",
        "_close_event",
    )

    def __init__(self, settings) -> None:
        from .common.config import Settings
        self._settings: Settings = settings

        self._buffer = _OfflineBuffer(
            settings.buffer_db_path, settings.buffer_max_bytes
        )
        self._outbox: asyncio.Queue[tuple[str, int, bytes]] = asyncio.Queue(
            maxsize=10_000
        )

        cid = f"{settings.mqtt_client_id_prefix}-{settings.station_id}-{new_ulid()[:6]}"
        # paho-mqtt v2.x: pass the CallbackAPIVersion enum (not the int 2)
        from paho.mqtt.client import CallbackAPIVersion
        self._client = PahoClient(
            client_id=cid,
            callback_api_version=CallbackAPIVersion.VERSION2,
            protocol=5,  # MQTT 5 — better back-pressure, reason codes
        )
        if settings.mqtt_username and settings.mqtt_password:
            self._client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
        if settings.mqtt_use_tls:
            ca = str(settings.mqtt_ca_cert) if settings.mqtt_ca_cert else None
            cert = str(settings.mqtt_client_cert) if settings.mqtt_client_cert else None
            key = str(settings.mqtt_client_key) if settings.mqtt_client_key else None
            self._client.tls_set(ca_certs=ca, certfile=cert, keyfile=key)

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect

        self._publisher_task: Optional[asyncio.Task] = None
        self._loop_task: Optional[asyncio.Task] = None
        self._drain_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._connected = False
        self._sent = 0
        self._dropped = 0
        self._close_event = asyncio.Event()

    # ---- lifecycle ---------------------------------------------------

    async def start(self) -> None:
        if self._publisher_task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._publisher_task = asyncio.create_task(self._publisher_loop())
        self._loop_task = asyncio.create_task(self._connect_loop())

    async def stop(self) -> None:
        self._close_event.set()
        for t in (self._publisher_task, self._loop_task, self._drain_task):
            if t is not None:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        self._publisher_task = None
        self._loop_task = None
        self._drain_task = None
        self._loop = None
        self._buffer.close()

    # ---- public API --------------------------------------------------

    async def publish(self, obs: RidObservation) -> None:
        """Queue an observation for publish.  Never raises."""
        await self.publish_message(
            self._settings.topic_obs,
            obs.to_mqtt_payload().encode("utf-8"),
        )

    async def publish_message(
        self,
        topic: str,
        payload: bytes,
        *,
        qos: int | None = None,
    ) -> None:
        """Queue any wire message through the same durable outbox."""
        qos = self._settings.mqtt_qos if qos is None else int(qos)
        try:
            self._outbox.put_nowait((topic, qos, payload))
        except asyncio.QueueFull:
            self._dropped += 1

    # ---- internals ---------------------------------------------------

    async def _connect_loop(self) -> None:
        attempt = 0
        while not self._close_event.is_set():
            try:
                self._client.connect_async(
                    self._settings.mqtt_host,
                    self._settings.mqtt_port,
                    keepalive=self._settings.mqtt_keepalive_s,
                )
                self._client.loop_start()
                # Wait for either disconnect or close
                await self._close_event.wait()
                return
            except Exception:
                delay = compute_delay(attempt, base_s=0.5, cap_s=30.0)
                attempt += 1
                await asyncio.sleep(delay)

    def _on_connect(self, client: PahoClient, userdata, flags, reason_code, properties=None) -> None:
        self._connected = (reason_code == 0)
        if self._connected and self._loop is not None:
            # paho calls this from its network thread, which has no event
            # loop.  Hand the drain back to the loop that owns the buffer.
            self._loop.call_soon_threadsafe(self._schedule_drain)

    def _schedule_drain(self) -> None:
        """Run on the publisher's event loop, never on paho's thread."""
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain_buffer())

    def _on_disconnect(self, client, userdata, flags=None, reason_code=None, properties=None) -> None:
        self._connected = False

    async def _publisher_loop(self) -> None:
        """Drain outbox to MQTT (or to disk buffer if not connected)."""
        while not self._close_event.is_set():
            try:
                topic, qos, payload = await asyncio.wait_for(
                    self._outbox.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            if not self._connected:
                # Offline: write to disk
                self._buffer.push(topic, qos, payload)
                continue

            try:
                if await self._publish_and_wait(topic, payload, qos):
                    self._sent += 1
                else:
                    self._buffer.push(topic, qos, payload)
            except Exception:
                self._buffer.push(topic, qos, payload)

    async def _drain_buffer(self) -> None:
        if not self._connected:
            return
        while True:
            rows = self._buffer.drain_batch(200)
            if not rows:
                return
            for seq, topic, qos, payload in rows:
                try:
                    if await self._publish_and_wait(topic, payload, qos):
                        self._sent += 1
                        self._buffer.delete(seq)
                    else:
                        return
                except Exception:
                    # Stay connected but stop draining; we'll retry.
                    return
            # Yield so the event loop can serve other tasks
            await asyncio.sleep(0)

    async def _publish_and_wait(self, topic: str, payload: bytes, qos: int) -> bool:
        """Publish and, for QoS 1/2, wait for the broker acknowledgement."""
        info = self._client.publish(topic, payload, qos=qos)
        if getattr(info, "rc", 0) != 0:
            return False
        if qos == 0:
            return True
        try:
            await asyncio.to_thread(
                info.wait_for_publish,
                int(self._settings.mqtt_ack_timeout_s),
            )
        except Exception:
            return False
        is_published = getattr(info, "is_published", None)
        return not callable(is_published) or bool(is_published())

    @property
    def stats(self) -> dict[str, int]:
        return {
            "sent": self._sent,
            "dropped": self._dropped,
            "buffered": self._buffer.size(),
            "connected": int(self._connected),
        }


__all__ = ["MQTTPublisher"]
