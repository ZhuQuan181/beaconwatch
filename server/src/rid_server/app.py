"""FastAPI application factory + lifespan."""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy import text

from .alerts import AlertEngine, AlertRing
from .bus import ObservationBus
from .config import get_settings
from .db import dispose_engine, get_session_factory
from .logging_setup import configure_logging, get_logger
from .metrics import RequestMetrics
from .mqtt_subscriber import MqttSubscriber
from .notifications import NotificationWorker
from .repository import (
    alert_to_out,
    list_alerts,
    notification_outbox_counts,
)
from .retention import RetentionWorker
from .routes import router, ws_router
from .security import RequestContextMiddleware, require_api_token
from .settings_state import set_subscriber_bus_hook

log = get_logger(__name__)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the MQTT subscriber; clean up the DB engine on shutdown."""
    settings = getattr(app.state, "settings", None) or get_settings()
    app.state.settings = settings
    configure_logging(settings.log_level)
    log.info("startup", env=settings.env, http_port=settings.http_port)

    bus = ObservationBus()
    app.state.bus = bus

    subscriber = MqttSubscriber(settings)
    app.state.subscriber = subscriber
    notifier = (
        NotificationWorker(settings)
        if settings.notifications_enabled
        else None
    )
    app.state.notifier = notifier
    if notifier is not None:
        await notifier.start()
    retention = (
        RetentionWorker(settings)
        if (
            settings.alert_retention_days > 0
            or settings.notification_retention_days > 0
        )
        else None
    )
    app.state.retention = retention
    if retention is not None:
        await retention.start()
    # Rules run after each committed observation.  Constructing the
    # engine here (not per message) keeps dedup state across messages —
    # a hovering drone would otherwise re-alert at 4 Hz.
    app.state.alerts = AlertRing(capacity=200)
    # The API reads the durable ledger directly, but keeping the recent
    # ring warm preserves the lightweight in-process view used by
    # operations and makes restart behavior predictable for any caller
    # that still reads app.state.alerts.
    try:
        factory = get_session_factory()
        async with factory() as session:
            recent = await list_alerts(session, limit=app.state.alerts.capacity)
        for row in reversed(recent):
            app.state.alerts.add(
                alert_to_out(row).model_dump(mode="json")
            )
    except Exception as exc:  # noqa: BLE001
        # A missing pre-migration table must not prevent the receiver
        # from starting; live alerts will still attempt persistence.
        log.warning("alerts.restore.failed", error=str(exc)[:200])
    engine = AlertEngine(
        max_alt_m=settings.alert_max_alt_m,
        unidentified=settings.alert_unidentified,
        dedup_seconds=settings.alert_dedup_seconds,
    )
    subscriber.alerts = engine
    # Wire the bus into the subscriber so persisted observations
    # fan out to WebSocket clients in real time.
    set_subscriber_bus_hook(
        subscriber,
        bus,
        app.state.alerts,
        settings=settings,
        wake_notifications=notifier.wake if notifier is not None else None,
    )
    await subscriber.start()
    try:
        yield
    finally:
        log.info("shutdown.begin")
        await subscriber.stop()
        if notifier is not None:
            await notifier.stop()
        if retention is not None:
            await retention.stop()
        await dispose_engine()
        log.info("shutdown.done")


def create_app() -> FastAPI:
    settings = get_settings()
    request_metrics = RequestMetrics()
    app = FastAPI(
        title="RID Server",
        version="0.7.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.metrics = request_metrics
    app.include_router(router, dependencies=[Depends(require_api_token)])
    app.include_router(ws_router)

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_methods=["GET", "POST", "PUT", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID", "X-Next-Cursor"],
            max_age=600,
        )
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
    app.add_middleware(RequestContextMiddleware, metrics=request_metrics)

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/live", tags=["meta"])
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/healthz", tags=["meta"], include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["meta"])
    async def health_ready() -> dict[str, str]:
        try:
            factory = get_session_factory()
            async with factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="database unavailable",
            ) from exc
        return {"status": "ready"}

    @app.get("/metrics", tags=["meta"], include_in_schema=False)
    async def get_metrics(request: Request) -> Response:
        subscriber = getattr(request.app.state, "subscriber", None)
        bus = getattr(request.app.state, "bus", None)
        notifier = getattr(request.app.state, "notifier", None)
        retention = getattr(request.app.state, "retention", None)
        values = {
            "rid_mqtt_connected": int(bool(getattr(subscriber, "connected", False))),
            "rid_mqtt_received_total": int(getattr(subscriber, "received", 0)),
            "rid_mqtt_persisted_total": int(getattr(subscriber, "persisted", 0)),
            "rid_mqtt_invalid_total": int(getattr(subscriber, "invalid", 0)),
            "rid_mqtt_reconnects_total": int(getattr(subscriber, "reconnects", 0)),
            "rid_mqtt_duplicates_total": int(getattr(subscriber, "duplicates", 0)),
            "rid_station_info_total": int(getattr(subscriber, "station_infos", 0)),
            "rid_ws_clients": int(getattr(bus, "subscriber_count", 0)),
            "rid_ws_dropped_frames_total": int(
                getattr(bus, "dropped_frames", 0)
            ),
            "rid_ws_delivered_frames_total": int(
                getattr(bus, "delivered_frames", 0)
            ),
            "rid_notification_worker_running": int(
                bool(getattr(notifier, "running", False))
            ),
            "rid_notification_last_success_timestamp_seconds": (
                notifier.last_success_at.timestamp()
                if getattr(notifier, "last_success_at", None) is not None
                else 0.0
            ),
            "rid_retention_worker_running": int(
                bool(getattr(retention, "running", False))
            ),
            "rid_retention_last_success_timestamp_seconds": (
                retention.last_success_at.timestamp()
                if getattr(retention, "last_success_at", None) is not None
                else 0.0
            ),
            "rid_retention_alerts_deleted_total": int(
                getattr(retention, "alerts_deleted", 0)
            ),
            "rid_retention_notifications_deleted_total": int(
                getattr(retention, "notifications_deleted", 0)
            ),
            "rid_retention_failures_total": int(
                getattr(retention, "failures", 0)
            ),
        }
        try:
            factory = get_session_factory()
            async with factory() as session:
                outbox = await notification_outbox_counts(session)
        except Exception:  # noqa: BLE001
            outbox = {
                "pending": 0,
                "delivering": 0,
                "delivered": 0,
                "dead": 0,
                "attempts": 0,
                "failures": 0,
            }
        values.update(
            {
                "rid_notification_outbox_pending": outbox["pending"],
                "rid_notification_outbox_delivering": outbox[
                    "delivering"
                ],
                "rid_notification_outbox_delivered": outbox["delivered"],
                "rid_notification_outbox_dead": outbox["dead"],
                "rid_notification_outbox_attempts_total": outbox[
                    "attempts"
                ],
                "rid_notification_outbox_failures_total": outbox[
                    "failures"
                ],
            }
        )
        lines = [f"{name} {value}" for name, value in values.items()]
        lines.extend(request.app.state.metrics.render())
        body = "\n".join(lines) + "\n"
        return Response(body, media_type="text/plain; version=0.0.4")

    # The WebSocket route needs the bus — bind it via app.state.
    # (We achieve this by reading app.state at request time, see routes.py.)
    return app


app = create_app()
