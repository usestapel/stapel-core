"""Persistent in-process subscriptions: a bus consumer with no process to run in.

On a broker a topic is read by a ``BaseBusConsumerCommand``. On an in-process
bus (the shipped ``MemoryBus`` default) no such command can run (core refuses
to start one), so a library subscribes the same handler here, from
``AppConfig.ready()``::

    from stapel_core.bus import subscribe_in_process

    subscribe_in_process(TOPIC, handle_event)

The registry lives in this module, not on a backend instance, so it survives
``reset_bus()`` and ``MemoryBus.clear()``. Only in-process backends read it:
``MemoryBus.publish`` delivers to it, synchronously and inside the publisher's
call. On a broker the subscription is inert and the command reads the topic.
A ``RoutingBus`` delivers through whichever backend owns the topic, so a topic
routed to ``memory`` is delivered in-process even when the router is not.

Handlers run inside the publisher's request and often inside its transaction.
A handler that must not act on rolled-back work defers itself
(``transaction.on_commit``); a handler that writes wraps itself in a savepoint
so its failure does not break the caller's transaction. A handler's exception
is logged by the backend and never reaches the publisher.
"""
from __future__ import annotations

import threading
from typing import Callable

from .event import Event

Handler = Callable[[Event], None]

_lock = threading.Lock()
_subscriptions: dict[str, list[Handler]] = {}


def subscribe_in_process(topic: str, handler: Handler) -> None:
    """Deliver every in-process publish to *topic* to *handler*. Idempotent."""
    with _lock:
        handlers = _subscriptions.setdefault(topic, [])
        if handler not in handlers:
            handlers.append(handler)


def unsubscribe_in_process(topic: str, handler: Handler) -> bool:
    """Remove *handler* from *topic*; True when it was subscribed."""
    with _lock:
        handlers = _subscriptions.get(topic, [])
        if handler not in handlers:
            return False
        handlers.remove(handler)
        return True


def in_process_subscribers(topic: str) -> tuple[Handler, ...]:
    """The handlers an in-process backend delivers *topic* to."""
    with _lock:
        return tuple(_subscriptions.get(topic, ()))


def carries_in_process(topic: str, bus=None) -> bool:
    """True when *topic* is delivered by an in-process backend.

    Asks the backend that actually carries the topic (``backend_for``), so a
    routing bus with an in-process route answers per topic.
    """
    if bus is None:
        from .router import get_bus

        bus = get_bus()
    backend_for = getattr(bus, "backend_for", None)
    if callable(backend_for):
        bus = backend_for(topic)
    return bool(getattr(bus, "in_process", False))


__all__ = [
    "carries_in_process",
    "in_process_subscribers",
    "subscribe_in_process",
    "unsubscribe_in_process",
]
