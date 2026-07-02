"""
Event envelope and event type constants.
"""

import json
import uuid
import time
from dataclasses import dataclass, field, asdict
class EventType:
    """Event type constants."""
    PROFILE_CHANGED = "profile.changed"
    NOTIFICATION_REQUESTED = "notification.requested"
    USER_CONTACT_CHANGED = "user.contact.changed"
    # DEPRECATED (0.3.x): the translate→notifications sync moved to the comm
    # Action "translations.changed"; no stapel module emits or consumes this
    # EventType anymore. Kept for deployments pinning the legacy Kafka
    # contract; do not use in new code.
    TRANSLATIONS_CHANGED = "translations.changed"


@dataclass
class Event:
    """
    Kafka event envelope.

    JSON format:
    {
        "event_type": "profile.changed",
        "event_id": "uuid-v4",
        "timestamp": 1707900000000,
        "service": "profiles",
        "version": 1,
        "payload": { ... }
    }
    """

    event_type: str
    service: str
    payload: dict = field(default_factory=dict)
    version: int = 1
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_json(self) -> str:
        """Serialize event to JSON string."""
        return json.dumps(asdict(self), default=str)

    def to_bytes(self) -> bytes:
        """Serialize event to UTF-8 bytes for Kafka."""
        return self.to_json().encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> "Event":
        """Deserialize event from Kafka message bytes."""
        d = json.loads(data.decode("utf-8"))
        return cls(
            event_type=d["event_type"],
            service=d["service"],
            payload=d.get("payload", {}),
            version=d.get("version", 1),
            event_id=d.get("event_id", ""),
            timestamp=d.get("timestamp", 0),
        )
