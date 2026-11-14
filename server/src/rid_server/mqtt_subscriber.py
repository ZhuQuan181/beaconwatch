"""Async MQTT subscriber that ingests edge observations into the DB."""
from __future__ import annotations

import asyncio
import contextlib
import json
import ssl
from datetime import datetime, timezone
from typing import Any

import aiomqtt
from pydantic import ValidationError
from sqlalchemy import select

from .alerts import AlertEngine, Zone
from .config import Settings, get_settings
from .db import GeofenceZone, get_session_factory
from .logging_setup import get_logger
from .models import DroneObservation, StationInfo
from .models import Station as StationModel
from .repository import touch_station, upsert_station, write_observation

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
        self._tls_context = self._build_tls_context()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        # Optional callback invoked after a successful DB persist.  The
        # FastAPI lifespan attaches the live ``ObservationBus`` here so
        # WebSocket clients can be notified.  In tests this stays None.
        self.on_persisted: Any = None
        # Same for alerts — a separate hook so a test can assert on
        # alerts without wiring a bus.
        self.on_alert: Any = None
        # Rule evaluation is optional: with no engine, ingest behaves
        # exactly as before.  Alerts run AFTER the row is committed, so
        # an alert always points at a row that exists.
        self.alerts: AlertEngine | None = None
        self._zones: list[Zone] = []
        self._zones_loaded_at: float = 0.0
        # Operational counters
        self.received = 0
        self.persisted = 0
        self.duplicates = 0
        self.station_infos = 0
        self.invalid = 0
        self.reconnects = 0
        self.connected = False

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
                    tls=self._settings.mqtt_tls,
                )
                try:
                    async with aiomqtt.Client(
                        hostname=self._settings.mqtt_host,
                        port=self._settings.mqtt_port,
                        username=self._settings.mqtt_username or None,
                        password=self._settings.mqtt_password or None,
                        identifier=self._settings.mqtt_client_id,
                        tls_context=self._tls_context,
                    ) as client:
                        await client.subscribe(
                            self._settings.mqtt_obs_topic,
                            qos=self._settings.mqtt_qos,
                        )
                        await client.subscribe(
                            self._settings.mqtt_info_topic,
                            qos=self._settings.mqtt_qos,
                        )
                        log.info(
                            "mqtt.subscribed",
                            obs_topic=self._settings.mqtt_obs_topic,
                            info_topic=self._settings.mqtt_info_topic,
                        )
                        self.connected = True
                        backoff = 1.0  # reset on successful connect
                        async for message in client.messages:
                            if self._stop.is_set():
                                break
                            await self.handle_message(message.topic, message.payload)
                finally:
                    self.connected = False
            except asyncio.CancelledError:
                raise
            except Exception as e:  # broker down, network blip, etc.
                self.connected = False
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
        topic_str = str(topic)
        topic_parts = topic_str.split("/")
        if len(topic_parts) == 4 and topic_parts[3] == "info":
            await self._handle_station_info(topic_str, payload)
            return

        station_id = self._extract_station_id(topic_str)
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
            inserted = await write_observation(session, obs)
        if not inserted:
            self.duplicates += 1
            log.debug(
                "obs.duplicate",
                drone_id=obs.drone_id,
                observation_id=obs.observation_id,
            )
            return
        self.persisted += 1

        # Evaluate rules only after the row is committed: an alert that
        # can't be traced back to a stored observation is worse than no
        # alert.  Failures here must never block ingest.
        if self.alerts is not None:
            try:
                zones = await self._load_zones()
                for alert in self.alerts.evaluate(obs, zones):
                    if self.on_alert is not None:
                        await self.on_alert(alert)
            except Exception as e:  # noqa: BLE001
                log.warning("alerts.eval.failed", error=str(e)[:200])

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

    async def _handle_station_info(self, topic: str, payload: bytes) -> None:
        try:
            data = json.loads(payload)
            info = StationInfo.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            self.invalid += 1
            log.warning("mqtt.invalid_info", topic=topic, error=str(exc)[:200])
            return

        topic_station = self._extract_station_id(topic)
        station_id = topic_station or info.station_id
        station = StationModel(
            station_id=station_id,
            name=info.name or station_id,
            lat=info.location.lat,
            lon=info.location.lon,
            alt_m=info.location.alt_m,
            firmware_version=info.firmware_version,
            last_seen=datetime.now(tz=timezone.utc),
            online=True,
        )
        factory = get_session_factory()
        async with factory() as session:
            await upsert_station(session, station)
        self.station_infos += 1
        log.info("station.info", station_id=station_id)

    async def _load_zones(self) -> list[Zone]:
        """Zones, cached for 30 s.

        Ingest runs at 4 Hz per drone; querying the zone table every
        message would dominate the write path.  A 30 s staleness window
        means a newly-drawn no-fly zone starts alerting within half a
        minute — fast enough for an operator, cheap enough for a hot loop.
        """
        import time as _time

        now = _time.monotonic()
        if self._zones and now - self._zones_loaded_at < 30.0:
            return self._zones
        factory = get_session_factory()
        async with factory() as session:
            rows = (await session.execute(select(GeofenceZone))).scalars().all()
        self._zones = [
            Zone(
                zone_id=r.zone_id,
                name=r.name,
                polygon=r.polygon,
                min_alt_m=r.min_alt_m,
                max_alt_m=r.max_alt_m,
                severity=r.severity,  # type: ignore[arg-type]
            )
            for r in rows
        ]
        self._zones_loaded_at = now
        return self._zones

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

    def _build_tls_context(self) -> ssl.SSLContext | None:
        """Build a verified TLS context when ``mqtt_tls`` is enabled."""
        if not self._settings.mqtt_tls:
            return None
        context = ssl.create_default_context(
            cafile=self._settings.mqtt_ca_cert or None
        )
        cert = self._settings.mqtt_client_cert
        key = self._settings.mqtt_client_key
        if bool(cert) != bool(key):
            raise ValueError(
                "RID_MQTT_CLIENT_CERT and RID_MQTT_CLIENT_KEY must be set together"
            )
        if cert and key:
            context.load_cert_chain(certfile=cert, keyfile=key)
        return context
