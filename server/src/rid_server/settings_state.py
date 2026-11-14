"""Module-level wiring helpers for the FastAPI app.

Why a module rather than passing the bus through ``MqttSubscriber.__init__``?
- The bus is created in the FastAPI lifespan, *after* import time.
- The subscriber is also created in the lifespan.
- A small module-level setter keeps the subscriber's API stable for tests
  (which construct ``MqttSubscriber(settings)`` without a bus).
- For real production wiring, see ``app.py::lifespan``.
"""
from __future__ import annotations

from .bus import ObservationBus
from .mqtt_subscriber import MqttSubscriber


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
        if ring is not None:
            ring.add(alert)
        await bus.publish_alert(alert)

    subscriber.on_persisted = _on_persisted
    subscriber.on_alert = _on_alert
