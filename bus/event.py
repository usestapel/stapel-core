"""
Bus event envelope — transport-agnostic.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field


@dataclass
class Event:
    """
    Message envelope for the bus.

    Attributes:
        event_type: Dot-separated topic string, e.g. ``profile.changed``.
        service:    Publishing service name, e.g. ``profiles``.
        payload:    Arbitrary JSON-serialisable dict.
        version:    Schema version — bump when payload shape changes.
        event_id:   UUID assigned at publish time.
        timestamp:  Unix milliseconds at publish time.
    """

    event_type: str
    service: str
    payload: dict = field(default_factory=dict)
    version: int = 1
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))
    # Routing key for partitioned transports (Kafka). Not serialised into payload.
    key: str | None = field(default=None, compare=False, repr=False)

    def to_json(self) -> str:
        d = asdict(self)
        d.pop("key", None)
        return json.dumps(d, default=str)

    def to_bytes(self) -> bytes:
        return self.to_json().encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> Event:
        d = json.loads(data.decode("utf-8"))
        return cls(
            event_type=d["event_type"],
            service=d["service"],
            payload=d.get("payload", {}),
            version=d.get("version", 1),
            event_id=d.get("event_id", ""),
            timestamp=d.get("timestamp", 0),
        )

    @classmethod
    def from_json(cls, data: str) -> Event:
        return cls.from_bytes(data.encode("utf-8"))
