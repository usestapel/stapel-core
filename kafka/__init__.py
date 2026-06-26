"""
Deprecated — use ``stapel_core.bus`` instead.
This module is kept for backwards compatibility and re-exports from stapel_core.bus.
"""
from stapel_core.bus import (  # noqa: F401
    Event,
    BaseBusConsumerCommand,
    get_bus,
)
from stapel_core.bus import publish as publish_event  # noqa: F401
from stapel_core.bus._config import KafkaBusConfig as KafkaConfig  # noqa: F401
from .events import EventType  # noqa: F401
from .topics import (  # noqa: F401
    TOPIC_PROFILE_CHANGED,
    TOPIC_NOTIFICATION_REQUESTED,
    TOPIC_USER_CONTACT_CHANGED,
    TOPIC_TRANSLATIONS_CHANGED,
)

# Legacy alias
BaseKafkaConsumerCommand = BaseBusConsumerCommand

__all__ = [
    "publish_event",
    "Event",
    "EventType",
    "BaseKafkaConsumerCommand",
    "BaseBusConsumerCommand",
    "KafkaConfig",
    "TOPIC_PROFILE_CHANGED",
    "TOPIC_NOTIFICATION_REQUESTED",
    "TOPIC_USER_CONTACT_CHANGED",
    "TOPIC_TRANSLATIONS_CHANGED",
]
