"""
Kafka integration for Iron services.

Public API:
    - publish_event: Publish an event to a Kafka topic
    - Event: Event envelope dataclass
    - EventType: Event type constants
    - BaseKafkaConsumerCommand: Base Django management command for Kafka consumers
    - KafkaConfig: Configuration dataclass
"""

from .events import Event, EventType
from .producer import publish_event
from .consumer import BaseKafkaConsumerCommand
from .config import KafkaConfig
from .topics import (
    TOPIC_PROFILE_CHANGED,
    TOPIC_NOTIFICATION_REQUESTED,
    TOPIC_USER_CONTACT_CHANGED,
    TOPIC_TRANSLATIONS_CHANGED,
)

__all__ = [
    "publish_event",
    "Event",
    "EventType",
    "BaseKafkaConsumerCommand",
    "KafkaConfig",
    "TOPIC_PROFILE_CHANGED",
    "TOPIC_NOTIFICATION_REQUESTED",
    "TOPIC_USER_CONTACT_CHANGED",
    "TOPIC_TRANSLATIONS_CHANGED",
]
