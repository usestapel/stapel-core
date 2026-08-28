"""A long-lived worker must start each unit of work on a live connection.

ironmemo, 2026-08-26 21:58 UTC → 2026-08-28: the notifications consumer had
been "Up 3 days" and had delivered nothing for 46 hours. Every event since
that timestamp failed with::

    psycopg2.InterfaceError: connection already closed

The database dropped the connection while the consumer sat idle. Nothing in
the loop ever reset it, so the SAME dead socket was reused for every later
event — including all four attempts of the retry loop, which is why retrying
could not help: a retry only helps when the next attempt can differ from the
last one. Meanwhile the HTTP layer answered "Verification code sent
successfully" to every OTP request, because publishing to the bus really did
succeed. Nobody could see the difference from outside.

The NATS backend and the function server already called
``close_old_connections()`` per unit of work; the Kafka path was the single
loop that did not. But ``close_old_connections()`` alone would not have been
enough either — see the first test below, which is the inversion control for
this whole change.
"""
import sys
import types

import pytest

from stapel_core.bus.event import Event
from stapel_core.django.db import close_stale_connections, worker_db_lifecycle


class FakeConnection:
    """Stands in for a DatabaseWrapper.

    NOT a shortcut: this suite runs on sqlite, whose backend defines
    ``is_usable()`` as ``return True`` — unconditionally, without touching the
    database. A test that dropped a real sqlite connection and then asserted
    that `close_stale_connections` noticed would be green on a mechanism that
    never ran, and red for the wrong reason (an in-memory sqlite database
    ceases to exist when its connection closes). The probe is the thing under
    test, so the probe's answer has to be something the test controls.
    """

    def __init__(self, *, usable=True, open_=True, in_atomic_block=False, probe_raises=False):
        self.connection = object() if open_ else None
        self.in_atomic_block = in_atomic_block
        self._usable = usable
        self._probe_raises = probe_raises
        self.probed = 0
        self.closed = False

    def is_usable(self):
        self.probed += 1
        if self._probe_raises:
            raise RuntimeError("connection already closed")
        return self._usable

    def close(self):
        self.closed = True
        self.connection = None


class FakeConnections:
    def __init__(self, *conns):
        self._conns = conns

    def all(self, initialized_only=False):
        return self._conns


@pytest.fixture
def db_connections(monkeypatch):
    """Install fake connections and a no-op `close_old_connections`."""
    import django.db

    installed = {}

    def install(*conns):
        handler = FakeConnections(*conns)
        monkeypatch.setattr(django.db, "connections", handler)
        monkeypatch.setattr(django.db, "close_old_connections", lambda: installed.setdefault("aged", True))
        return handler

    install.calls = installed
    return install


class TestCloseStaleConnections:
    def test_a_connection_that_stopped_answering_is_closed(self, db_connections):
        dead = FakeConnection(usable=False)
        db_connections(dead)
        close_stale_connections()
        assert dead.closed

    def test_a_probe_that_raises_is_the_answer_not_an_error(self, db_connections):
        """`InterfaceError` from the probe means unusable — propagating it
        would turn the guard itself into the thing that DLQs the event."""
        dead = FakeConnection(probe_raises=True)
        db_connections(dead)
        close_stale_connections()
        assert dead.closed

    def test_a_healthy_connection_is_left_alone(self, db_connections):
        live = FakeConnection(usable=True)
        db_connections(live)
        close_stale_connections()
        assert not live.closed
        assert live.probed == 1

    def test_a_connection_nobody_opened_is_not_probed(self, db_connections):
        idle = FakeConnection(open_=False)
        db_connections(idle)
        close_stale_connections()
        assert idle.probed == 0
        assert not idle.closed

    def test_a_connection_inside_a_transaction_is_never_touched(self, db_connections):
        """Closing mid-transaction would discard the transaction — a worse
        outcome than the stale connection this exists to fix."""
        busy = FakeConnection(usable=False, in_atomic_block=True)
        db_connections(busy)
        close_stale_connections()
        assert not busy.closed
        assert busy.probed == 0

    def test_ageing_out_still_happens(self, db_connections):
        """The probe is added to `close_old_connections`, not instead of it:
        CONN_MAX_AGE and error-flagged connections must still be honoured."""
        install = db_connections
        install(FakeConnection())
        close_stale_connections()
        assert install.calls.get("aged") is True

    def test_the_lifecycle_releases_afterwards(self, db_connections):
        """A connection the handler poisoned must not be inherited by the
        next event."""
        install = db_connections
        install(FakeConnection())
        with worker_db_lifecycle():
            pass
        assert install.calls.get("aged") is True


class FakeMessage:
    def __init__(self, payload: bytes, topic: str = "a.topic"):
        self._payload = payload
        self._topic = topic

    def error(self):
        return None

    def value(self):
        return self._payload

    def topic(self):
        return self._topic


class FakeConsumer:
    """Delivers one message, then stops the loop."""

    def __init__(self, config):
        self.committed: list[FakeMessage] = []
        self.closed = False
        self._delivered = False

    def subscribe(self, topics):
        self.topics = topics

    def poll(self, timeout=None):
        if self._delivered:
            FakeConsumer.running.clear()
            return None
        self._delivered = True
        return FakeConsumer.message

    def commit(self, msg):
        self.committed.append(msg)

    def close(self):
        self.closed = True


@pytest.fixture
def kafka(monkeypatch):
    """Drive the real `KafkaBus.consume` loop with a fake broker."""
    from stapel_core.bus.backends import kafka as kafka_module

    class FakeKafkaError:
        _PARTITION_EOF = object()
        UNKNOWN_TOPIC_OR_PART = object()

    package = types.ModuleType("confluent_kafka")
    package.Consumer = FakeConsumer
    package.KafkaError = FakeKafkaError
    admin = types.ModuleType("confluent_kafka.admin")
    admin.AdminClient = lambda config: types.SimpleNamespace(
        list_topics=lambda timeout=None: types.SimpleNamespace(topics={"a.topic": 1, "a.topic.dlq": 1}),
        create_topics=lambda new_topics: {},
    )
    admin.NewTopic = lambda *a, **kw: None
    package.admin = admin
    monkeypatch.setitem(sys.modules, "confluent_kafka", package)
    monkeypatch.setitem(sys.modules, "confluent_kafka.admin", admin)

    # The loop's own signal handlers and watchdog are irrelevant here and
    # cannot run off the main thread.
    monkeypatch.setattr(kafka_module.signal, "signal", lambda *a, **kw: None)
    monkeypatch.setattr(kafka_module.KafkaBus, "_start_watchdog", lambda self, running: None)
    monkeypatch.setattr(kafka_module.time, "sleep", lambda seconds: None)

    return kafka_module


class TestKafkaConsumeHygiene:
    def _run(self, kafka_module, handler, monkeypatch):
        import threading

        running = threading.Event()
        running.set()
        FakeConsumer.running = running
        FakeConsumer.message = FakeMessage(
            Event(event_type="otp.requested", service="auth", payload={}).to_bytes()
        )
        monkeypatch.setattr(threading, "Event", lambda: running)
        bus = kafka_module.KafkaBus()
        bus.consume(["a.topic"], "group", handler, poll_timeout=0)

    def test_hygiene_runs_before_every_attempt(self, kafka, monkeypatch):
        """The order is the whole content: a reset that happens only AFTER
        the first attempt still loses the first event, and the first event
        of an outage is somebody's login code."""
        order: list[str] = []
        monkeypatch.setattr(
            "stapel_core.django.db.close_stale_connections",
            lambda: order.append("hygiene"),
        )
        attempts = {"n": 0}

        def handler(event):
            order.append("handler")
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("connection already closed")

        self._run(kafka, handler, monkeypatch)
        assert order[:4] == ["hygiene", "handler", "hygiene", "handler"]

    def test_an_event_that_needed_a_fresh_connection_is_not_dlqd(self, kafka, monkeypatch):
        dlq: list = []
        monkeypatch.setattr(
            kafka.KafkaBus, "_send_to_dlq", lambda self, topic, event: dlq.append(event) or True
        )
        attempts = {"n": 0}

        def handler(event):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("connection already closed")

        self._run(kafka, handler, monkeypatch)
        assert dlq == []
        assert attempts["n"] == 2
