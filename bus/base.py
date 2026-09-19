"""
Abstract bus backend.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

from .event import Event

#: Seconds ``flush()`` waits by default; ``STAPEL_BUS_FLUSH_TIMEOUT`` (env or
#: Django setting) overrides it for the process-wide entry points.
DEFAULT_FLUSH_TIMEOUT = 5.0


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

    def flush(self, timeout: float = DEFAULT_FLUSH_TIMEOUT) -> int:
        """Wait up to *timeout* seconds for queued messages to be delivered.

        Returns how many are STILL undelivered when the wait ends — 0 means
        the transport confirmed every one of them.

        ``publish()`` is fire-and-forget, and for an asynchronous producer
        that is literally true: librdkafka accepts the message into a local
        queue and a background thread delivers it, so ``publish()`` returning
        says nothing about any broker having seen it. A process that exits
        right afterwards takes the queue with it —

            Producer terminating with 1 message (464 bytes) still in queue

        — which is a message lost, not delayed, and the commonest shape of it
        is a management command that emits after a commit and returns. So the
        ack has to be waitable, and every caller that says "delivered" on the
        strength of a publish (the outbox relay, first of all) has to wait for
        it first.

        The default is for backends whose ``publish()`` already returns on the
        broker's acknowledgement (memory, Redis Streams, NATS JetStream's
        awaited PubAck): nothing is in flight when it returns, so there is
        nothing to wait for and the answer is 0. Asynchronous producers
        override it.
        """
        return 0
