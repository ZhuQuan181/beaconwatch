"""Module-level wiring helpers for the FastAPI app.

Why a module rather than passing the bus through ``MqttSubscriber.__init__``?
- The bus is created in the FastAPI lifespan, *after* import time.
- The subscriber is also created in the lifespan.
- A small module-level setter keeps the subscriber's API stable for tests
  (which construct ``MqttSubscriber(settings)`` without a bus).
- For real production wiring, see ``app.py::lifespan``.
"""
from __future__ import annotations

from . import repository
from .bus import ObservationBus
from .db import get_session_factory
from .logging_setup import get_logger
from .mqtt_subscriber import MqttSubscriber

log = get_logger(__name__)


def set_subscriber_bus_hook(
    subscriber: MqttSubscriber, bus: ObservationBus, ring=None
) -> None:
    """Attach publish callbacks that the subscriber invokes per message.

    Kept as free functions (not methods on the subscriber) so the
    subscriber itself remains unaware of the bus in tests.

    Both streams share one bus: a client needs them in arrival order, and
    a separate alert channel would let an alert overtake the observation
    that triggered it.
    """
    async def _on_persisted(obs) -> None:  # type: ignore[no-untyped-def]
        await bus.publish(obs)

    async def _on_alert(alert) -> None:  # type: ignore[no-untyped-def]
        payload = repository.transient_alert_to_out(alert).model_dump(
            mode="json"
        )
        try:
            factory = get_session_factory()
            async with factory() as session:
                row = await repository.upsert_alert(session, alert)
            payload = repository.alert_to_out(row).model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001
            # Alert persistence is valuable but must never suppress the
            # live alert or break observation ingest.  The fallback keeps
            # the operator panel working while the failure is logged.
            log.warning(
                "alert.persist.failed",
                alert_id=getattr(alert, "alert_id", ""),
                error=str(exc)[:200],
            )
        if ring is not None:
            ring.add(payload)
        await bus.publish_alert(payload)

    subscriber.on_persisted = _on_persisted
    subscriber.on_alert = _on_alert
