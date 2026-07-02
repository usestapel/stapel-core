"""Coverage tests for bus/backends/nats.py (publish/consume), bus/consumer.py,
bus/router.py and the dispatch_outbox management command."""
import asyncio as real_asyncio
import sys
import types
from io import StringIO

import pytest
from django.core.management import call_command

from stapel_core.bus.consumer import BaseBusConsumerCommand
from stapel_core.bus.event import Event
from stapel_core.bus.router import SHORTHANDS, _resolve_backend_path


# ---------------------------------------------------------------------------
# fake nats module tree
# ---------------------------------------------------------------------------


class FakeNatsTimeoutError(Exception):
    pass


class FakeJS:
    def __init__(self):
        self.streams = []
        self.published = []
        self.pull_subs = []
        self.fail_next_publish = False
        self.add_stream_error = None

    async def add_stream(self, name=None, subjects=None):
        if self.add_stream_error is not None:
            raise self.add_stream_error
        self.streams.append((name, subjects))

    async def publish(self, subject, payload, headers=None):
        if self.fail_next_publish:
            self.fail_next_publish = False
            raise RuntimeError("dlq write failed")
        self.published.append((subject, payload, headers))

    async def pull_subscribe(self, subject, durable=None, stream=None, config=None):
        self.pull_subs.append({"durable": durable, "stream": stream, "config": config})
        return self.sub


class FakeNC:
    is_closed = False

    def __init__(self, js):
        self._js = js
        self.drained = False

    def jetstream(self):
        return self._js

    async def drain(self):
        self.drained = True


def _install_fake_nats(monkeypatch, js):
    nc = FakeNC(js)
    connect_calls = []

    fake_nats = types.ModuleType("nats")
    fake_errors = types.ModuleType("nats.errors")
    fake_errors.TimeoutError = FakeNatsTimeoutError
    fake_js_mod = types.ModuleType("nats.js")
    fake_api_mod = types.ModuleType("nats.js.api")

    class ConsumerConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_api_mod.ConsumerConfig = ConsumerConfig
    fake_nats.errors = fake_errors
    fake_js_mod.api = fake_api_mod
    fake_nats.js = fake_js_mod

    async def connect(url, **kwargs):
        connect_calls.append(url)
        return nc

    fake_nats.connect = connect
    monkeypatch.setitem(sys.modules, "nats", fake_nats)
    monkeypatch.setitem(sys.modules, "nats.errors", fake_errors)
    monkeypatch.setitem(sys.modules, "nats.js", fake_js_mod)
    monkeypatch.setitem(sys.modules, "nats.js.api", fake_api_mod)
    return nc, connect_calls


@pytest.fixture(autouse=True)
def _clean_nats_env(monkeypatch):
    monkeypatch.delenv("STAPEL_NATS_EVENT_PREFIX", raising=False)
    monkeypatch.delenv("STAPEL_NATS_STREAM", raising=False)
    monkeypatch.delenv("NATS_URL", raising=False)


# ---------------------------------------------------------------------------
# NatsJetStreamBus.publish
# ---------------------------------------------------------------------------


def _new_bus():
    from stapel_core.bus.backends.nats import NatsJetStreamBus

    return NatsJetStreamBus()


def test_publish_connects_once_and_sets_msg_id(monkeypatch):
    js = FakeJS()
    _, connect_calls = _install_fake_nats(monkeypatch, js)
    bus = _new_bus()

    event = Event(event_type="user.deleted", service="gdpr", payload={"u": 1})
    bus.publish("user.deleted", event)
    event2 = Event(event_type="user.deleted", service="gdpr", payload={"u": 2})
    bus.publish("user.deleted", event2)

    assert connect_calls == ["nats://nats:4222"]  # reused on second publish
    assert js.streams == [("stapel-events", ["stapel.evt.>"])]
    assert len(js.published) == 2
    subject, payload, headers = js.published[0]
    assert subject == "stapel.evt.user.deleted"
    assert Event.from_bytes(payload).payload == {"u": 1}
    assert headers == {"Nats-Msg-Id": event.event_id}


def test_publish_tolerates_existing_stream(monkeypatch):
    js = FakeJS()
    js.add_stream_error = RuntimeError("stream exists")
    _install_fake_nats(monkeypatch, js)
    bus = _new_bus()
    bus.publish("a.b", Event(event_type="a.b", service="s", payload={}))
    assert js.published[0][0] == "stapel.evt.a.b"


# ---------------------------------------------------------------------------
# NatsJetStreamBus.consume — full loop with scripted fetches
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_consume_ack_dlq_and_nak_cycle(monkeypatch):
    from stapel_core.bus.backends import nats as nats_backend

    monkeypatch.setattr(nats_backend.time, "sleep", lambda s: None)

    created_events = []

    class AsyncioProxy:
        def __getattr__(self, item):
            return getattr(real_asyncio, item)

        def Event(self):
            evt = real_asyncio.Event()
            created_events.append(evt)
            return evt

    monkeypatch.setattr(nats_backend, "asyncio", AsyncioProxy())

    js = FakeJS()
    nc, _ = _install_fake_nats(monkeypatch, js)

    class FakeMsg:
        def __init__(self, data):
            self.data = data
            self.acked = False
            self.naks = []

        async def ack(self):
            self.acked = True

        async def nak(self, delay=None):
            self.naks.append(delay)

    ok_event = Event(event_type="user.deleted", service="gdpr", payload={"u": "1"})
    ok_msg = FakeMsg(ok_event.to_bytes())
    poison_msg = FakeMsg(b"\xffnot json")
    poison_msg_nak = FakeMsg(b"\xffstill not json")

    fetch_script = [
        ("msgs", [ok_msg]),                     # handled -> ack
        ("raise", real_asyncio.TimeoutError()),  # asyncio timeout -> continue
        ("msgs", [poison_msg]),                 # poison -> DLQ publish -> ack
        ("raise", FakeNatsTimeoutError()),      # nats timeout -> continue
        ("fail_dlq", [poison_msg_nak]),         # DLQ publish fails -> nak
        ("stop", None),
    ]

    class FakeSub:
        def __init__(self):
            self.calls = 0

        async def fetch(self, batch=10, timeout=5):
            action, value = fetch_script[self.calls]
            self.calls += 1
            if action == "raise":
                raise value
            if action == "fail_dlq":
                js.fail_next_publish = True
                return value
            if action == "stop":
                created_events[0].set()  # the `stopping` event inside _consume
                raise FakeNatsTimeoutError()
            return value

    js.sub = FakeSub()

    seen = []
    bus = _new_bus()
    bus.consume(["user.deleted", "payment.completed"], "iron.notifications", seen.append)

    assert [e.payload for e in seen] == [{"u": "1"}]
    assert ok_msg.acked is True
    # poison message parked in the DLQ with a deterministic msg-id
    dlq = [p for p in js.published if p[0].endswith(".dlq")]
    assert len(dlq) == 1
    subject, payload, headers = dlq[0]
    assert subject == "stapel.evt.__undecodable__.dlq"
    wrapper = Event.from_bytes(payload)
    assert wrapper.event_type == "__undecodable__"
    assert headers == {"Nats-Msg-Id": wrapper.event_id + ".dlq"}
    assert poison_msg.acked is True
    # failed DLQ write -> message nak'd for redelivery
    assert poison_msg_nak.acked is False
    assert poison_msg_nak.naks == [5]
    # consumer config derived from group / topics
    assert js.pull_subs[0]["durable"] == "iron_notifications"
    assert js.pull_subs[0]["config"].kwargs["filter_subjects"] == [
        "stapel.evt.user.deleted",
        "stapel.evt.payment.completed",
    ]
    assert nc.drained is True


# ---------------------------------------------------------------------------
# bus/consumer.py — BaseBusConsumerCommand
# ---------------------------------------------------------------------------


class _DemoConsumer(BaseBusConsumerCommand):
    topics = ["profile.changed"]
    consumer_group = "notifications"


def test_consumer_command_handle(monkeypatch):
    import stapel_core.bus.consumer as consumer_mod

    recorded = {}

    class FakeBus:
        def consume(self, topics, group, handler, *, poll_timeout):
            recorded.update(
                topics=topics, group=group, handler=handler, poll_timeout=poll_timeout
            )

    monkeypatch.setattr(consumer_mod, "get_bus", lambda: FakeBus())

    buf = StringIO()
    cmd = _DemoConsumer(stdout=buf)
    parser = cmd.create_parser("manage.py", "demo_consumer")
    options = parser.parse_args(["--poll-timeout", "0.7"])
    cmd.handle(**vars(options))

    assert recorded["topics"] == ["profile.changed"]
    assert recorded["group"] == "notifications"
    assert recorded["poll_timeout"] == 0.7
    assert "Starting consumer group=notifications" in buf.getvalue()

    with pytest.raises(NotImplementedError):
        recorded["handler"](Event(event_type="profile.changed", service="s"))


def test_consumer_command_default_poll_timeout():
    cmd = _DemoConsumer(stdout=StringIO())
    parser = cmd.create_parser("manage.py", "demo_consumer")
    options = parser.parse_args([])
    assert options.poll_timeout == 0.1


# ---------------------------------------------------------------------------
# bus/router.py — remaining branches
# ---------------------------------------------------------------------------


def test_resolve_defaults_to_kafka_without_env_or_setting(monkeypatch, settings):
    monkeypatch.delenv("STAPEL_BUS_BACKEND", raising=False)
    del settings.STAPEL_BUS_BACKEND
    assert _resolve_backend_path() == SHORTHANDS["kafka"]


def test_resolve_survives_broken_settings(monkeypatch):
    import django.conf

    monkeypatch.delenv("STAPEL_BUS_BACKEND", raising=False)

    class BrokenSettings:
        def __getattribute__(self, name):
            raise RuntimeError("settings unavailable")

    monkeypatch.setattr(django.conf, "settings", BrokenSettings())
    assert _resolve_backend_path() == SHORTHANDS["kafka"]


def test_get_bus_rechecks_singleton_under_lock(monkeypatch):
    from stapel_core.bus import router

    sentinel = object()

    class TrickLock:
        def __enter__(self):
            router._bus = sentinel

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(router, "_bus", None)
    monkeypatch.setattr(router, "_lock", TrickLock())
    assert router.get_bus() is sentinel


# ---------------------------------------------------------------------------
# dispatch_outbox management command
# ---------------------------------------------------------------------------


def test_dispatch_outbox_once_reports_counts(monkeypatch):
    from stapel_core.django.outbox.management.commands import dispatch_outbox as cmd_mod

    calls = []
    monkeypatch.setattr(
        cmd_mod, "dispatch_pending", lambda limit: calls.append(limit) or (2, 1)
    )
    buf = StringIO()
    call_command("dispatch_outbox", "--once", "--batch", "25", stdout=buf)
    assert calls == [25]
    assert "outbox: delivered=2 failed=1" in buf.getvalue()


def test_dispatch_outbox_once_silent_when_idle(monkeypatch):
    from stapel_core.django.outbox.management.commands import dispatch_outbox as cmd_mod

    monkeypatch.setattr(cmd_mod, "dispatch_pending", lambda limit: (0, 0))
    buf = StringIO()
    call_command("dispatch_outbox", "--once", stdout=buf)
    assert buf.getvalue() == ""


def test_dispatch_outbox_loops_and_sleeps(monkeypatch):
    from stapel_core.django.outbox.management.commands import dispatch_outbox as cmd_mod

    class _StopLoop(Exception):
        pass

    passes = []
    monkeypatch.setattr(
        cmd_mod, "dispatch_pending", lambda limit: passes.append(limit) or (0, 0)
    )
    slept = []

    def fake_sleep(seconds):
        slept.append(seconds)
        if len(slept) >= 2:
            raise _StopLoop()

    monkeypatch.setattr(cmd_mod.time, "sleep", fake_sleep)
    with pytest.raises(_StopLoop):
        call_command("dispatch_outbox", "--interval", "0.25", stdout=StringIO())
    assert slept == [0.25, 0.25]
    assert passes == [100, 100]  # default batch, one pass per loop iteration
