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
