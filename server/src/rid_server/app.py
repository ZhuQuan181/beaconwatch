"""FastAPI application factory + lifespan."""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI

from .alerts import AlertEngine, AlertRing
from .bus import ObservationBus
from .config import get_settings
from .db import dispose_engine
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
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(router)

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # The WebSocket route needs the bus — bind it via app.state.
    # (We achieve this by reading app.state at request time, see routes.py.)
    return app


app = create_app()
