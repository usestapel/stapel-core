"""Bus flush: the broker ack a short-lived process must wait for.

Two defects meet here. ``KafkaBus.publish`` hands the message to librdkafka's
background queue and returns; the outbox then marked its row dispatched on
that return, before any broker acknowledged anything. A management command
that exits right after the commit therefore lost the message outright —
librdkafka says so on the way out:

    Producer terminating with 1 message (464 bytes) still in queue

and the row is marked sent, so the relay never sweeps it up. Delivery is only
confirmed once the producer's delivery reports have come back, which is what
``flush(timeout)`` waits for.
"""
from __future__ import annotations

import logging

import pytest
from django.utils import timezone

from stapel_core.bus import flush, get_bus, publish, reset_bus
from stapel_core.bus.base import BusBackend
from stapel_core.bus.backends.memory import MemoryBus
from stapel_core.bus.event import Event
from stapel_core.django.outbox import relay
from stapel_core.django.outbox.models import OutboxEvent


class _QueueingBus(BusBackend):
    """A producer-shaped backend: publish() queues, flush() reports the queue.

    The shape of every asynchronous producer (librdkafka above all): the
    message is accepted into a local queue and the transport confirms it
    later. ``remaining`` is what a real ``Producer.flush(timeout)`` returns —
    the number of messages STILL not delivered when the timeout ran out.
    """

    def __init__(self, remaining: int = 0) -> None:
        self.queued: list[Event] = []
        self.remaining = remaining
        self.flushes: list[float] = []

    def publish(self, topic: str, event: Event) -> None:
        self.queued.append(event)

    def consume(self, topics, group, handler, *, poll_timeout: float = 0.1) -> None:
        raise NotImplementedError

    def flush(self, timeout: float = 5.0) -> int:
        self.flushes.append(timeout)
        return self.remaining


@pytest.fixture
def queueing_bus(settings, monkeypatch):
    """Install a _QueueingBus as THE bus, with the bus action transport."""
    from stapel_core.bus import router

    bus = _QueueingBus()
    monkeypatch.setattr(router, "_bus", bus)
    settings.STAPEL_COMM = {"ACTION_TRANSPORT": "bus"}
    return bus


# ---------------------------------------------------------------------------
# The backend contract
# ---------------------------------------------------------------------------


def test_backend_flush_defaults_to_nothing_pending():
    """A synchronous backend has nothing to wait for — the default says 0."""

    class Synchronous(BusBackend):
        def publish(self, topic, event):  # pragma: no cover - never called
            pass

        def consume(self, topics, group, handler, *, poll_timeout=0.1):
            pass  # pragma: no cover - never called

    assert Synchronous().flush(1.0) == 0


def test_memory_bus_flush_is_zero():
    assert MemoryBus().flush(0.1) == 0


def test_kafka_bus_flush_forwards_to_the_producer():
    from stapel_core.bus.backends.kafka import KafkaBus

    class _Producer:
        def __init__(self):
            self.timeouts = []

        def flush(self, timeout):
            self.timeouts.append(timeout)
            return 2

    bus = KafkaBus()
    assert bus.flush(1.0) == 0  # no producer was ever created
    bus._producer = _Producer()
    assert bus.flush(2.5) == 2
    assert bus._producer.timeouts == [2.5]


def test_routing_bus_flush_sums_its_live_backends():
    from stapel_core.bus.backends.routing import RoutingBus

    import threading

    bus = RoutingBus.__new__(RoutingBus)
    bus._lock = threading.Lock()
    bus._backends = {"a": _QueueingBus(remaining=1), "b": _QueueingBus(remaining=2)}
    assert bus.flush(1.0) == 3


# ---------------------------------------------------------------------------
# The public entry point
# ---------------------------------------------------------------------------


def test_public_flush_does_not_create_a_bus():
    """flush() in a process that never published must stay a no-op.

    Creating the backend here would connect to a broker on the way out of a
    process that never used one.
    """
    from stapel_core.bus import router

    reset_bus()
    assert flush(0.1) == 0
    assert router._bus is None


def test_public_flush_warns_naming_what_stayed_behind(caplog, monkeypatch):
    from stapel_core.bus import router

    monkeypatch.setattr(router, "_bus", _QueueingBus(remaining=3))
    with caplog.at_level(logging.WARNING, logger="stapel_core.bus.router"):
        assert flush(0.5) == 3
    assert "3 message" in caplog.text


def test_public_flush_passes_the_configured_timeout(monkeypatch, settings):
    from stapel_core.bus import router

    bus = _QueueingBus()
    monkeypatch.setattr(router, "_bus", bus)
    settings.STAPEL_BUS_FLUSH_TIMEOUT = "2"
    assert flush() == 0
    assert bus.flushes == [2.0]


def test_atexit_flush_is_registered_once_when_the_bus_is_created(monkeypatch):
    from stapel_core.bus import router

    registered = []
    monkeypatch.setattr(router.atexit, "register", lambda fn: registered.append(fn))
    monkeypatch.setattr(router, "_atexit_registered", False)
    reset_bus()
    get_bus()
    reset_bus()
    get_bus()
    assert len(registered) == 1


def test_atexit_flush_drains_the_live_bus(monkeypatch, caplog):
    from stapel_core.bus import router

    bus = _QueueingBus(remaining=1)
    monkeypatch.setattr(router, "_bus", bus)
    with caplog.at_level(logging.WARNING, logger="stapel_core.bus.router"):
        router._flush_at_exit()
    assert bus.flushes  # the exiting process waited for the broker
    assert "1 message" in caplog.text


def test_atexit_flush_never_raises(monkeypatch):
    """Process exit is not a place to raise — a broken broker must not
    turn a finished job into a traceback and a non-zero exit code."""
    from stapel_core.bus import router

    class _Broken(_QueueingBus):
        def flush(self, timeout: float = 5.0) -> int:
            raise RuntimeError("broker gone")

    monkeypatch.setattr(router, "_bus", _Broken())
    router._flush_at_exit()  # must not raise


# ---------------------------------------------------------------------------
# The outbox: dispatched_at means DELIVERED, not "handed to a queue"
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_row_is_not_marked_dispatched_before_the_broker_acks(queueing_bus):
    """The lost-message bug: produce() returned, the row said "sent"."""
    queueing_bus.remaining = 1  # the message is still in the producer queue
    row = OutboxEvent.objects.create(
        topic="user.created",
        event_json=Event(event_type="user.created", service="t", payload={}).to_json(),
    )

    assert relay.dispatch_one(row.pk) is False

    row.refresh_from_db()
    assert row.dispatched_at is None  # still the relay's to re-send
    assert row.attempts == 1
    assert "not confirmed" in row.last_error


@pytest.mark.django_db(transaction=True)
def test_row_is_marked_dispatched_once_the_broker_acks(queueing_bus):
    row = OutboxEvent.objects.create(
        topic="user.created",
        event_json=Event(event_type="user.created", service="t", payload={}).to_json(),
    )

    assert relay.dispatch_one(row.pk) is True

    row.refresh_from_db()
    assert row.dispatched_at is not None
    assert queueing_bus.flushes  # it waited before saying so


@pytest.mark.django_db(transaction=True)
def test_dispatch_pending_confirms_the_whole_batch_before_marking_it(queueing_bus):
    """One flush per sweep, not one per row — and nothing is marked until it
    comes back clean."""
    for i in range(3):
        OutboxEvent.objects.create(
            topic="user.created",
            event_json=Event(
                event_type="user.created", service="t", payload={"i": i}
            ).to_json(),
        )

    delivered, failed = relay.dispatch_pending()
    assert (delivered, failed) == (3, 0)
    assert len(queueing_bus.flushes) == 1
    assert OutboxEvent.objects.filter(dispatched_at__isnull=True).count() == 0


@pytest.mark.django_db(transaction=True)
def test_dispatch_pending_retries_the_batch_the_broker_did_not_confirm(queueing_bus):
    queueing_bus.remaining = 2
    for i in range(2):
        OutboxEvent.objects.create(
            topic="user.created",
            event_json=Event(
                event_type="user.created", service="t", payload={"i": i}
            ).to_json(),
        )
    before = timezone.now()

    delivered, failed = relay.dispatch_pending()

    assert (delivered, failed) == (0, 2)
    for row in OutboxEvent.objects.all():
        assert row.dispatched_at is None
        assert row.attempts == 1
        assert row.next_attempt_at >= before + relay._backoff(1)


@pytest.mark.django_db(transaction=True)
def test_first_chance_dispatch_waits_for_the_ack(queueing_bus):
    """The seam a client host hit: emit() in a command that then exits."""
    from stapel_core.comm import mutate_and_emit

    with mutate_and_emit() as emit_event:
        emit_event("user.created", {"user_id": "u1"})

    # on_commit ran the first-chance dispatch on the way out of the block
    assert queueing_bus.flushes  # which confirmed delivery before marking
    assert OutboxEvent.objects.get().dispatched_at is not None


def test_publish_then_flush_is_the_documented_pair(monkeypatch):
    from stapel_core.bus import router

    bus = _QueueingBus()
    monkeypatch.setattr(router, "_bus", bus)
    publish("t", Event(event_type="t", service="s", payload={}))
    assert flush(1.0) == 0
    assert len(bus.queued) == 1
