"""Async MQTT subscriber that ingests edge observations into the DB."""
from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime
from typing import Any

import aiomqtt
from pydantic import ValidationError

from .config import Settings, get_settings
from .db import get_session_factory
from .logging_setup import get_logger
from .models import DroneObservation
from .repository import touch_station, write_observation

log = get_logger(__name__)


class MqttSubscriber:
    """Long-lived subscriber with auto-reconnect.

    Lifecycle: :meth:`start` is called by FastAPI's lifespan; the
    subscribe loop runs as a background task.  On disconnect the loop
    sleeps (with backoff) and reconnects; the task is *not* cancelled
    until :meth:`stop` is called.

    For unit tests, we expose :meth:`handle_message` which is fully
    synchronous and side-effect free apart from the DB write.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        # Optional callback invoked after a successful DB persist.  The
        # FastAPI lifespan attaches the live ``ObservationBus`` here so
        # WebSocket clients can be notified.  In tests this stays None.
        self.on_persisted: Any = None
        # Operational counters
        self.received = 0
        self.persisted = 0
        self.invalid = 0
        self.reconnects = 0

    # ── Lifecycle ─────────────────────────────────────────────────
    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="mqtt-subscriber")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        self._task = None

    # ── Reconnect loop ────────────────────────────────────────────
    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                log.info(
                    "mqtt.connect", host=self._settings.mqtt_host,
                    port=self._settings.mqtt_port,
                )
                async with aiomqtt.Client(
                    hostname=self._settings.mqtt_host,
                    port=self._settings.mqtt_port,
                    username=self._settings.mqtt_username or None,
                    password=self._settings.mqtt_password or None,
                    identifier=self._settings.mqtt_client_id,
                ) as client:
                    await client.subscribe(
                        self._settings.mqtt_obs_topic,
                        qos=self._settings.mqtt_qos,
                    )
                    log.info(
                        "mqtt.subscribed",
                        topic=self._settings.mqtt_obs_topic,
                    )
                    backoff = 1.0  # reset on successful connect
                    async for message in client.messages:
                        if self._stop.is_set():
                            break
                        await self.handle_message(message.topic, message.payload)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # broker down, network blip, etc.
                self.reconnects += 1
                log.warning(
                    "mqtt.disconnected",
                    error=str(e),
                    backoff=backoff,
                    reconnects=self.reconnects,
                )
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                backoff = min(backoff * 2.0, 60.0)

    # ── Per-message handler ───────────────────────────────────────
    async def handle_message(self, topic: Any, payload: bytes) -> None:
        """Parse + persist a single MQTT message.

        Public so it can be unit-tested without a broker.
        """
        self.received += 1
        station_id = self._extract_station_id(str(topic))
        try:
            data = json.loads(payload)
            # Normalize protocol casing
            if "protocol" in data and isinstance(data["protocol"], str):
                data["protocol"] = data["protocol"].lower()
            obs = DroneObservation.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as e:
            self.invalid += 1
            log.warning(
                "mqtt.invalid",
                topic=str(topic),
                error=str(e)[:200],
            )
            return

        # Trust the topic station_id over the payload's, since the edge
        # can lie about its own ID.  (Topic is set by the broker, not
        # the client — except see note in security audit.)
        if station_id and station_id != obs.station_id:
            obs = obs.model_copy(update={"station_id": station_id})

        factory = get_session_factory()
        async with factory() as session:
            # Upsert station FIRST so observations FK is satisfied.
            # ``touch_station`` is an idempotent upsert.
            await touch_station(session, obs.station_id, online=True)
            await write_observation(session, obs)
        self.persisted += 1
        # Fan out to WebSocket clients via the bus, if wired.
        if self.on_persisted is not None:
            try:
                await self.on_persisted(obs)
            except Exception as e:  # never let fan-out kill the subscriber
                log.warning("ws.fanout.failed", error=str(e)[:200])
        log.debug(
            "obs.persisted",
            drone_id=obs.drone_id,
            station_id=obs.station_id,
        )

    @staticmethod
    def _extract_station_id(topic: str) -> str | None:
        """``rid/v1/<station_id>/obs`` → ``<station_id>``.

        Returns None on malformed topics (rather than raising) so a
        single bad publisher can't kill the subscriber.

        The version segment is deliberately not pinned to ``v1``: the
        e2e harness runs a second subscriber against ``rid/v2/...`` to
        stay off the production topic, and hard-coding the version here
        made every such message arrive with ``station_id=None``.
        """
        parts = topic.split("/")
        # expected: ['rid', '<version>', '<station_id>', 'obs']
        if len(parts) != 4 or parts[0] != "rid":
            return None
        if not parts[1].startswith("v"):
            return None
        return parts[2]
