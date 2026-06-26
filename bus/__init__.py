"""
stapel_core.bus — transport-agnostic message bus.

Public API:
    publish(topic, event)         — send an event
    get_bus()                     — get the configured backend instance
    reset_bus()                   — force re-init (tests)
    Event                         — message envelope dataclass
    BusBackend                    — ABC for custom backends
    BaseBusConsumerCommand        — base Django management command for consumers

Backend is set via Django setting:
    STAPEL_BUS_BACKEND = "stapel_core.bus.backends.kafka.KafkaBus"   # default/prod
    STAPEL_BUS_BACKEND = "stapel_core.bus.backends.memory.MemoryBus"  # tests/dev
"""

from .base import BusBackend
from .consumer import BaseBusConsumerCommand
from .event import Event
from .router import get_bus, reset_bus


def publish(topic: str, event: Event) -> None:
    """Publish *event* to *topic* via the configured backend."""
    get_bus().publish(topic, event)


__all__ = [
    "publish",
    "get_bus",
    "reset_bus",
    "Event",
    "BusBackend",
    "BaseBusConsumerCommand",
]
