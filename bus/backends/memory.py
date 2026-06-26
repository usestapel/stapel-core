"""
In-memory bus backend — for tests and local dev without a broker.

Published events are stored in ``MemoryBus.events`` so tests can assert on them:

    from stapel_core.bus import get_bus
    bus = get_bus()
    assert bus.events[-1].event_type == "profile.changed"
"""
from __future__ import annotations

import logging
import queue
import threading
from collections import defaultdict
from typing import Callable

from ..base import BusBackend
from ..event import Event

logger = logging.getLogger(__name__)


class MemoryBus(BusBackend):
    """
    Thread-safe in-memory bus. Subscribers are registered per topic.
    ``publish()`` delivers synchronously to all registered handlers,
    then appends to ``self.events`` for test introspection.
    """

    def __init__(self) -> None:
        self.events: list[Event] = []
        self._subscribers: dict[str, list[Callable[[Event], None]]] = defaultdict(list)
        self._queue: queue.Queue[Event] = queue.Queue()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # BusBackend interface
    # ------------------------------------------------------------------

    def publish(self, topic: str, event: Event) -> None:
        logger.debug("MemoryBus.publish topic=%s event_id=%s", topic, event.event_id)
        with self._lock:
            self.events.append(event)
        for handler in self._subscribers.get(topic, []):
            try:
                handler(event)
            except Exception:
                logger.exception("MemoryBus handler error topic=%s", topic)
        self._queue.put(event)

    def consume(
        self,
        topics: list[str],
        group: str,
        handler: Callable[[Event], None],
        *,
        poll_timeout: float = 0.1,
    ) -> None:
        """Drain the queue, calling *handler* for each event whose type matches *topics*."""
        logger.debug("MemoryBus.consume topics=%s group=%s", topics, group)
        try:
            while True:
                event = self._queue.get(timeout=poll_timeout)
                if event.event_type in topics:
                    handler(event)
        except queue.Empty:
            pass

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def subscribe(self, topic: str, handler: Callable[[Event], None]) -> None:
        """Register a synchronous handler called on every publish to *topic*."""
        self._subscribers[topic].append(handler)

    def clear(self) -> None:
        """Reset state between tests."""
        with self._lock:
            self.events.clear()
            self._subscribers.clear()
        while not self._queue.empty():
            self._queue.get_nowait()
