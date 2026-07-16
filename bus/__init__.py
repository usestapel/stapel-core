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
    STAPEL_BUS_BACKEND = "stapel_core.bus.backends.memory.MemoryBus"  # default
    STAPEL_BUS_BACKEND = "stapel_core.bus.backends.kafka.KafkaBus"    # explicit opt-in
"""

import logging

from .base import BusBackend
from .consumer import BaseBusConsumerCommand
from .event import Event
from .router import get_bus, reset_bus

logger = logging.getLogger(__name__)


def publish(topic: str, event: Event) -> None:
    """Publish *event* to *topic* via the configured backend.

    A missing transport library (``STAPEL_BUS_BACKEND=kafka`` without
    ``confluent-kafka`` installed, the ``nats`` equivalent, …) raises
    ``ImportError`` deep inside the backend on the *first* publish call —
    easy to miss if the caller fail-softs on publish errors (as
    ``notifications.request_notification`` does, by contract). Log it loudly
    here, at error level with a fix-it hint, before letting it propagate —
    callers still decide whether the failure is fatal, but it is never
    silent. ``stapel_core.bus.checks`` catches the same misconfiguration
    earlier, at ``manage.py check`` / boot-smoke time.
    """
    try:
        get_bus().publish(topic, event)
    except ImportError as exc:
        logger.error(
            "bus.publish failed: backend transport library not importable "
            "(%s) — topic=%r event_type=%r. The configured STAPEL_BUS_BACKEND "
            "needs its extra installed, e.g. pip install 'stapel-core[kafka]' "
            "or 'stapel-core[nats]'.",
            exc, topic, event.event_type,
        )
        raise


__all__ = [
    "publish",
    "get_bus",
    "reset_bus",
    "Event",
    "BusBackend",
    "BaseBusConsumerCommand",
]
