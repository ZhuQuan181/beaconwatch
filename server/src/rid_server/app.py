"""FastAPI application factory + lifespan."""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException, Request, Response, status
from sqlalchemy import text

from .alerts import AlertEngine, AlertRing
from .bus import ObservationBus
from .config import get_settings
from .db import dispose_engine, get_session_factory
from .logging_setup import configure_logging, get_logger
from .mqtt_subscriber import MqttSubscriber
from .routes import router
from .settings_state import set_subscriber_bus_hook

log = get_logger(__name__)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the MQTT subscriber; clean up the DB engine on shutdown."""
    settings = get_settings()
    configure_logging(settings.log_level)
    log.info("startup", env=settings.env, http_port=settings.http_port)

    bus = ObservationBus()
    app.state.bus = bus

    subscriber = MqttSubscriber(settings)
    app.state.subscriber = subscriber
    # Rules run after each committed observation.  Constructing the
    # engine here (not per message) keeps dedup state across messages —
    # a hovering drone would otherwise re-alert at 4 Hz.
    app.state.alerts = AlertRing(capacity=200)
    engine = AlertEngine(
        max_alt_m=settings.alert_max_alt_m,
        unidentified=settings.alert_unidentified,
        dedup_seconds=settings.alert_dedup_seconds,
    )
    subscriber.alerts = engine
    # Wire the bus into the subscriber so persisted observations
    # fan out to WebSocket clients in real time.
    set_subscriber_bus_hook(subscriber, bus, app.state.alerts)
    await subscriber.start()
    try:
        yield
    finally:
        log.info("shutdown.begin")
        await subscriber.stop()
        await dispose_engine()
        log.info("shutdown.done")


def create_app() -> FastAPI:
    app = FastAPI(
        title="RID Server",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.include_router(router)

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
    async def metrics(request: Request) -> Response:
        subscriber = getattr(request.app.state, "subscriber", None)
        bus = getattr(request.app.state, "bus", None)
        values = {
            "rid_mqtt_connected": int(bool(getattr(subscriber, "connected", False))),
            "rid_mqtt_received_total": int(getattr(subscriber, "received", 0)),
            "rid_mqtt_persisted_total": int(getattr(subscriber, "persisted", 0)),
            "rid_mqtt_invalid_total": int(getattr(subscriber, "invalid", 0)),
            "rid_mqtt_reconnects_total": int(getattr(subscriber, "reconnects", 0)),
            "rid_station_info_total": int(getattr(subscriber, "station_infos", 0)),
            "rid_ws_clients": int(getattr(bus, "subscriber_count", 0)),
        }
        body = "\n".join(f"{name} {value}" for name, value in values.items()) + "\n"
        return Response(body, media_type="text/plain; version=0.0.4")

    # The WebSocket route needs the bus — bind it via app.state.
    # (We achieve this by reading app.state at request time, see routes.py.)
    return app


app = create_app()
