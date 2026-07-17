"""
Event contract constants (``EventType``, ``TOPIC_*``).

Transport lives in :mod:`stapel_core.bus`; this package only carries the
cross-service event-type strings (:mod:`stapel_core.kafka.events`) and topic
names (:mod:`stapel_core.kafka.topics`).
"""
from .events import EventType  # noqa: F401
from .topics import (  # noqa: F401
    TOPIC_NOTIFICATION_REQUESTED,
    TOPIC_PROFILE_CHANGED,
    TOPIC_USER_CONTACT_CHANGED,
)

__all__ = [
    "EventType",
    "TOPIC_PROFILE_CHANGED",
    "TOPIC_NOTIFICATION_REQUESTED",
    "TOPIC_USER_CONTACT_CHANGED",
]
