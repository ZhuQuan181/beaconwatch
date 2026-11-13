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
    subscriber: MqttSubscriber, bus: ObservationBus
) -> None:
    """Attach a publish callback that the subscriber invokes per message.

    Kept as a free function (not a method on the subscriber) so the
    subscriber itself remains unaware of the bus in tests.
    """
    async def _on_persisted(obs) -> None:  # type: ignore[no-untyped-def]
        await bus.publish(obs)

    subscriber.on_persisted = _on_persisted
