"""One place that records "this event was given up on".

A dead-letter queue is where a system puts the things it could not do. That
makes DLQ depth the single most load-bearing number a bus deployment has —
and it is exactly the number nobody has, because parking an event is a
handful of lines inside each backend's retry loop and every backend spells
it differently.

ironmemo, 2026-08-25 → 2026-08-28: eight login codes were parked here, one
after another, over two and a half days. Every one was logged at ERROR. The
containers stayed "Up", the HTTP layer kept answering "Verification code sent
successfully" because publishing to the bus really had succeeded, and the
outage ended when a human happened to look. The signal existed; nothing
counted it, so nothing could alarm on it.

So: one function, called by every backend, that both logs the giving-up in a
consistent shape and increments a counter a deployment can alert on. A
backend that parks an event without calling this is invisible in exactly the
way the outage was.
"""
from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .event import Event

logger = logging.getLogger(__name__)

#: The metric a deployment alerts on. Any non-zero rate means the system is
#: dropping work on the floor.
DLQ_METRIC = "bus_dlq_total"


def record_parked(topic: str, event: "Event | None" = None, *, reason: str = "handler") -> None:
    """Count one event parked in the dead-letter queue for *topic*.

    ``reason`` separates the two ways an event ends up here, because they need
    different answers: ``"handler"`` is code that failed on a message the bus
    understood, ``"undecodable"`` is a message the bus could not even read
    (a producer/consumer format split, not a bug in the handler).

    Never raises: the caller is already on a failure path, and a metrics
    backend that is unavailable must not turn a parked event into a crash.
    """
    event_type = getattr(event, "event_type", None) or "unknown"
    try:
        from ..observability import metrics

        metrics.counter(
            DLQ_METRIC,
            labels={"topic": topic, "event_type": event_type, "reason": reason},
            description="Events parked in a dead-letter queue (work given up on)",
        )
    except Exception:  # pragma: no cover - the facade already guards itself
        logger.debug("bus: DLQ metric not recorded", exc_info=True)
    # The traceback is how this outage was actually diagnosed, so it is not
    # dropped in favour of a tidy line: attached when we are inside an
    # exception handler (every handler-failure call is), absent otherwise.
    logger.error(
        "bus: event parked in DLQ topic=%s event_type=%s event_id=%s reason=%s "
        "(alert on the %s metric — a non-zero rate is work being dropped)",
        topic, event_type, getattr(event, "event_id", None), reason, DLQ_METRIC,
        exc_info=sys.exc_info()[0] is not None,
    )
