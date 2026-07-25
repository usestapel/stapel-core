"""
Abstract bus backend.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

from .event import Event


class BusBackend(ABC):
    """
    Transport-agnostic message bus.

    Implementations: MemoryBus (tests), KafkaBus (prod).
    Configured via ``STAPEL_BUS_BACKEND`` Django setting.
    """

    #: True when publisher and consumer must share one process (the queue
    #: lives in memory). Standalone consumer commands refuse to run on such
    #: a backend — see BaseBusConsumerCommand — because they would drain an
    #: empty queue, exit, and be restarted forever by the container runtime.
    in_process: bool = False

    @abstractmethod
    def publish(self, topic: str, event: Event) -> None:
        """Publish *event* to *topic*. Fire-and-forget."""

    @abstractmethod
    def consume(
        self,
        topics: list[str],
        group: str,
        handler: Callable[[Event], None],
        *,
        poll_timeout: float = 0.1,
    ) -> None:
        """
        Block indefinitely, calling *handler* for each incoming event.
        Implementations are responsible for retry, DLQ, and graceful shutdown.
        """
