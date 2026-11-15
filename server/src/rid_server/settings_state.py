"""Module-level wiring helpers for the FastAPI app.

Why a module rather than passing the bus through ``MqttSubscriber.__init__``?
- The bus is created in the FastAPI lifespan, *after* import time.
- The subscriber is also created in the lifespan.
- A small module-level setter keeps the subscriber's API stable for tests
  (which construct ``MqttSubscriber(settings)`` without a bus).
- For real production wiring, see ``app.py::lifespan``.
"""
from __future__ import annotations

from collections.abc import Callable

from . import repository
from .bus import ObservationBus
from .config import Settings
from .db import get_session_factory
from .logging_setup import get_logger
from .mqtt_subscriber import MqttSubscriber

log = get_logger(__name__)


def set_subscriber_bus_hook(
    subscriber: MqttSubscriber,
    bus: ObservationBus,
    ring=None,
    *,
    settings: Settings | None = None,
    wake_notifications: Callable[[], None] | None = None,
) -> None:
    """Attach publish callbacks that the subscriber invokes per message.

    Kept as free functions (not methods on the subscriber) so the
    subscriber itself remains unaware of the bus in tests.

    Both streams share one bus: a client needs them in arrival order, and
    a separate alert channel would let an alert overtake the observation
    that triggered it.
    """
    notify = bool(settings and settings.notifications_enabled)

    async def _on_persisted(obs) -> None:  # type: ignore[no-untyped-def]
        await bus.publish(obs)

    async def _on_alert(alert) -> None:  # type: ignore[no-untyped-def]
        payload = repository.transient_alert_to_out(alert).model_dump(
            mode="json"
        )
        persisted = False
        suppressed = False
        try:
            factory = get_session_factory()
            async with factory() as session:
                row = await repository.upsert_alert(
                    session,
                    alert,
                    enqueue_notification=notify,
                    dedup_seconds=(
                        settings.alert_dedup_seconds
                        if settings is not None
                        else None
                    ),
                )
            if row is None:
                suppressed = True
            else:
                payload = repository.alert_to_out(row).model_dump(mode="json")
                persisted = True
        except Exception as exc:  # noqa: BLE001
            # Alert persistence is valuable but must never suppress the
            # live alert or break observation ingest.  The fallback keeps
            # the operator panel working while the failure is logged.
            log.warning(
                "alert.persist.failed",
                alert_id=getattr(alert, "alert_id", ""),
                error=str(exc)[:200],
            )
        if suppressed:
            log.debug(
                "alert.suppressed_duplicate",
                alert_id=getattr(alert, "alert_id", ""),
            )
            return
        if persisted and notify and wake_notifications is not None:
            try:
                wake_notifications()
            except Exception as exc:  # noqa: BLE001
                log.warning("notification.wake.failed", error=str(exc)[:200])
        if ring is not None:
            ring.add(payload)
        await bus.publish_alert(payload)

    subscriber.on_persisted = _on_persisted
    subscriber.on_alert = _on_alert
