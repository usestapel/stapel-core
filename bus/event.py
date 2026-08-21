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
        key:        Routing/partition key for partitioned transports (Kafka).
    """

    event_type: str
    service: str
    payload: dict = field(default_factory=dict)
    version: int = 1
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))
    # Routing key for partitioned transports (Kafka). Serialised in the
    # envelope (not folded into `payload`) so it survives every hop that
    # round-trips through to_json/from_json — notably the outbox, which
    # stores event.to_json() and later re-hydrates it for delivery. Losing
    # it there used to make KafkaBus.publish() fall back to the random
    # event_id, silently degrading per-key ordering to round-robin.
    key: str | None = field(default=None, compare=False, repr=False)

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)

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
            key=d.get("key"),
        )

    @classmethod
    def from_json(cls, data: str) -> Event:
        return cls.from_bytes(data.encode("utf-8"))
