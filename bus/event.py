"""
Bus event envelope — transport-agnostic.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field


def _ambient(key: str) -> str:
    """The named trace id in flight, or ``""``.

    Imported lazily and defensively: the envelope must stay constructible in
    a process that never configured observability (and in one where the
    import graph is only half-built, e.g. during migrations), and an
    unfilled correlation field is never worth an exception on a publish.
    """
    try:
        from ..observability.context import current_trace

        return getattr(current_trace(), key, "") or ""
    except Exception:  # pragma: no cover - defensive
        return ""


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

    # ── trace correlation (docs/pending/data-storage-and-observability-v2 §2)
    #
    # Filled from the ambient trace context at construction time, so an
    # emit inside a request inherits the request's ids with no call site
    # passing anything, and a handler's own emits inherit the ids the
    # delivery bound. Empty when nothing started a trace — an unconfigured
    # service publishes exactly what it published before.
    #
    # compare=False/repr=False deliberately: two events are the same event
    # because of what they say, not because of which trace observed them.
    # Equality assertions written before correlation existed keep holding.
    #
    # trace_id       the whole distributed operation
    # span_id        this hop of it
    # correlation_id the business operation (may outlive one trace)
    # causation_id   the message that caused this one — turns a fan-out into
    #                a tree instead of a bag of same-trace events
    trace_id: str = field(
        default_factory=lambda: _ambient("trace_id"), compare=False, repr=False
    )
    span_id: str = field(
        default_factory=lambda: _ambient("span_id"), compare=False, repr=False
    )
    correlation_id: str = field(
        default_factory=lambda: _ambient("correlation_id"),
        compare=False,
        repr=False,
    )
    causation_id: str = field(
        default_factory=lambda: _ambient("causation_id"),
        compare=False,
        repr=False,
    )

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
            # Restored explicitly, not defaulted: rehydrating an envelope
            # must not re-stamp it with the CURRENT process's trace. That is
            # exactly the outbox relay's situation — it reads a row minutes
            # later, in another process, and the event still belongs to the
            # operation that wrote it. Absent (an envelope published before
            # this field existed) reads as "no trace", never as this one.
            trace_id=d.get("trace_id", "") or "",
            span_id=d.get("span_id", "") or "",
            correlation_id=d.get("correlation_id", "") or "",
            causation_id=d.get("causation_id", "") or "",
        )

    @classmethod
    def from_json(cls, data: str) -> Event:
        return cls.from_bytes(data.encode("utf-8"))
