"""Tests for the Redis Streams bus backend.

Uses fakeredis (in-memory, real command semantics) instead of a live Redis
server — XADD/XREADGROUP/XACK/XAUTOCLAIM behave the same way a real server
would, so this exercises the actual publish/consume/ack/reclaim/DLQ contract,
not just mocked calls.
"""
from __future__ import annotations

import fakeredis
import pytest

from stapel_core.bus.event import Event
from stapel_core.bus.router import SHORTHANDS, _resolve_backend_path, get_bus, reset_bus


def _client():
    return fakeredis.FakeRedis(decode_responses=False)


def _bus(client=None):
    from stapel_core.bus.backends.redis_streams import RedisStreamsBus

    bus = RedisStreamsBus()
    bus._client = client if client is not None else _client()
    return bus


# ---------------------------------------------------------------------------
# Backend selection: shorthand + alias resolve to the same dotted path
# ---------------------------------------------------------------------------


def test_redis_streams_shorthand_resolves():
    assert (
        SHORTHANDS["redis_streams"]
        == "stapel_core.bus.backends.redis_streams.RedisStreamsBus"
    )


def test_redis_alias_resolves_to_same_backend():
    assert SHORTHANDS["redis"] == SHORTHANDS["redis_streams"]


def test_env_selects_redis_streams(monkeypatch):
    monkeypatch.setenv("STAPEL_BUS_BACKEND", "redis_streams")
    assert _resolve_backend_path() == SHORTHANDS["redis_streams"]


def test_env_selects_redis_alias(monkeypatch):
    monkeypatch.setenv("STAPEL_BUS_BACKEND", "redis")
    assert _resolve_backend_path() == SHORTHANDS["redis"]


def test_get_bus_instantiates_redis_streams_backend(monkeypatch):
    """Constructing the backend must not touch the network — connection is
    lazy (first publish()/consume() call), same contract as Kafka/NATS."""
    from stapel_core.bus.backends.redis_streams import RedisStreamsBus

    monkeypatch.setenv("STAPEL_BUS_BACKEND", "redis_streams")
    reset_bus()
    try:
        assert isinstance(get_bus(), RedisStreamsBus)
    finally:
        reset_bus()


# ---------------------------------------------------------------------------
# Consumer naming
# ---------------------------------------------------------------------------


def test_consumer_name_includes_group_host_pid():
    from stapel_core.bus.backends.redis_streams import _consumer_name

    name = _consumer_name("notifications")
    assert name.startswith("notifications:")
    assert name.count(":") == 2


def test_consumer_name_differs_per_group():
    from stapel_core.bus.backends.redis_streams import _consumer_name

    assert _consumer_name("group-a") != _consumer_name("group-b")


# ---------------------------------------------------------------------------
# DLQ naming
# ---------------------------------------------------------------------------


def test_dlq_topic_for():
    from stapel_core.bus.backends.redis_streams import dlq_topic_for

    assert dlq_topic_for("user.deleted") == "user.deleted.dlq"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_url_falls_back_to_redis_url(monkeypatch):
    from stapel_core.bus._config import RedisStreamsBusConfig

    monkeypatch.delenv("STAPEL_REDIS_BUS_URL", raising=False)
    monkeypatch.setenv("REDIS_URL", "redis://cache-host:6379/2")
    assert RedisStreamsBusConfig.url() == "redis://cache-host:6379/2"


def test_config_dedicated_bus_url_wins_over_redis_url(monkeypatch):
    from stapel_core.bus._config import RedisStreamsBusConfig

    monkeypatch.setenv("REDIS_URL", "redis://cache-host:6379/2")
    monkeypatch.setenv("STAPEL_REDIS_BUS_URL", "redis://bus-host:6379/0")
    assert RedisStreamsBusConfig.url() == "redis://bus-host:6379/0"


def test_config_claim_idle_ms_default(monkeypatch):
    from stapel_core.bus._config import RedisStreamsBusConfig

    monkeypatch.delenv("STAPEL_REDIS_BUS_CLAIM_IDLE_MS", raising=False)
    assert RedisStreamsBusConfig.claim_idle_ms() == 60000


def test_config_maxlen_default(monkeypatch):
    from stapel_core.bus._config import RedisStreamsBusConfig

    monkeypatch.delenv("STAPEL_REDIS_BUS_STREAM_MAXLEN", raising=False)
    assert RedisStreamsBusConfig.maxlen() == 100000


# ---------------------------------------------------------------------------
# Publish / consumer-group creation
# ---------------------------------------------------------------------------


def test_publish_xadds_to_stream_named_after_topic():
    bus = _bus()
    event = Event(event_type="user.deleted", service="gdpr", payload={"user_id": "u1"})
    bus.publish("user.deleted", event)

    entries = bus._client.xrange("user.deleted", "-", "+")
    assert len(entries) == 1
    _id, fields = entries[0]
    assert Event.from_bytes(fields[b"data"]).event_id == event.event_id


def test_ensure_group_is_idempotent():
    bus = _bus()
    bus._ensure_group(bus._client, "topic.a", "group.a")
    # Second call must not raise (BUSYGROUP swallowed).
    bus._ensure_group(bus._client, "topic.a", "group.a")


def test_ensure_group_creates_stream_even_if_empty():
    bus = _bus()
    bus._ensure_group(bus._client, "topic.b", "group.b")
    assert bus._client.exists("topic.b")


# ---------------------------------------------------------------------------
# Publish -> XREADGROUP -> handle -> XACK round trip
# ---------------------------------------------------------------------------


def test_publish_subscribe_round_trip_acks_on_success():
    bus = _bus()
    client = bus._client
    topic, group = "orders.created", "billing"
    bus._ensure_group(client, topic, group)

    event = Event(event_type=topic, service="orders", payload={"order_id": "o1"})
    bus.publish(topic, event)

    resp = client.xreadgroup(group, "consumer-1", {topic: ">"}, count=10, block=100)
    [(_stream, messages)] = resp
    assert len(messages) == 1
    msg_id, fields = messages[0]

    received = []
    bus._handle(client, topic, group, msg_id, fields, received.append)

    assert received[0].event_id == event.event_id
    pending = client.xpending(topic, group)
    assert pending["pending"] == 0  # acked


def test_handle_retries_then_succeeds(monkeypatch):
    from stapel_core.bus.backends import redis_streams as backend

    monkeypatch.setattr(backend.time, "sleep", lambda s: None)
    bus = _bus()
    client = bus._client
    topic, group = "payment.completed", "billing"
    bus._ensure_group(client, topic, group)
    event = Event(event_type=topic, service="billing", payload={})
    bus.publish(topic, event)

    [(_stream, [(msg_id, fields)])] = client.xreadgroup(
        group, "consumer-1", {topic: ">"}, count=10, block=100
    )

    calls = {"n": 0}

    def flaky(evt):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")

    bus._handle(client, topic, group, msg_id, fields, flaky)

    assert calls["n"] == 3
    assert client.xpending(topic, group)["pending"] == 0


def test_handle_exhausted_retries_goes_to_dlq_and_acks_original(monkeypatch):
    from stapel_core.bus.backends import redis_streams as backend

    monkeypatch.setattr(backend.time, "sleep", lambda s: None)
    bus = _bus()
    client = bus._client
    topic, group = "payment.completed", "billing"
    bus._ensure_group(client, topic, group)
    event = Event(event_type=topic, service="billing", payload={})
    bus.publish(topic, event)

    [(_stream, [(msg_id, fields)])] = client.xreadgroup(
        group, "consumer-1", {topic: ">"}, count=10, block=100
    )

    def always_fails(evt):
        raise RuntimeError("permanent")

    bus._handle(client, topic, group, msg_id, fields, always_fails)

    # Original stream's PEL is cleared (acked) once the DLQ send succeeded.
    assert client.xpending(topic, group)["pending"] == 0

    dlq_entries = client.xrange("payment.completed.dlq", "-", "+")
    assert len(dlq_entries) == 1
    dlq_event = Event.from_bytes(dlq_entries[0][1][b"data"])
    assert dlq_event.event_id == event.event_id


def test_handle_poison_message_goes_to_dlq_raw():
    bus = _bus()
    client = bus._client
    topic, group = "user.deleted", "gdpr"
    bus._ensure_group(client, topic, group)
    msg_id = client.xadd(topic, {"data": b"\xff not json"})
    client.xreadgroup(group, "consumer-1", {topic: ">"}, count=10, block=100)

    bus._handle(client, topic, group, msg_id, {b"data": b"\xff not json"}, lambda e: None)

    assert client.xpending(topic, group)["pending"] == 0
    dlq_entries = client.xrange("user.deleted.dlq", "-", "+")
    assert len(dlq_entries) == 1
    wrapper = Event.from_bytes(dlq_entries[0][1][b"data"])
    assert wrapper.event_type == "__undecodable__"


# ---------------------------------------------------------------------------
# Pending redelivery after XAUTOCLAIM (crashed-consumer recovery)
# ---------------------------------------------------------------------------


def test_reclaim_pending_redelivers_after_crashed_consumer():
    bus = _bus()
    client = bus._client
    topic, group = "profile.changed", "search-index"
    bus._ensure_group(client, topic, group)

    event = Event(event_type=topic, service="profiles", payload={"id": "p1"})
    bus.publish(topic, event)

    # "consumer-dead" reads the message and then vanishes without acking —
    # the entry stays in the group's PEL, attributed to it.
    client.xreadgroup(group, "consumer-dead", {topic: ">"}, count=10, block=100)
    assert client.xpending(topic, group)["pending"] == 1

    received = []
    # idle_ms=0 — any pending time counts as stale for this test.
    bus._reclaim_pending(client, [topic], group, "consumer-survivor", 0, received.append)

    assert received[0].event_id == event.event_id
    # Reclaimed, handled successfully, and acked — nothing left pending.
    assert client.xpending(topic, group)["pending"] == 0


def test_reclaim_pending_leaves_fresh_entries_alone():
    """A message still within the idle window (its original consumer might
    just be slow, not dead) must not be claimed."""
    bus = _bus()
    client = bus._client
    topic, group = "profile.changed", "search-index"
    bus._ensure_group(client, topic, group)
    bus.publish(topic, Event(event_type=topic, service="profiles", payload={}))
    client.xreadgroup(group, "consumer-busy", {topic: ">"}, count=10, block=100)

    received = []
    # A huge idle threshold — nothing this fresh qualifies.
    bus._reclaim_pending(client, [topic], group, "consumer-other", 3_600_000, received.append)

    assert received == []
    assert client.xpending(topic, group)["pending"] == 1


def test_reclaim_pending_failed_handler_goes_to_dlq(monkeypatch):
    from stapel_core.bus.backends import redis_streams as backend

    monkeypatch.setattr(backend.time, "sleep", lambda s: None)
    bus = _bus()
    client = bus._client
    topic, group = "profile.changed", "search-index"
    bus._ensure_group(client, topic, group)
    event = Event(event_type=topic, service="profiles", payload={})
    bus.publish(topic, event)
    client.xreadgroup(group, "consumer-dead", {topic: ">"}, count=10, block=100)

    def always_fails(evt):
        raise RuntimeError("still broken after reclaim")

    bus._reclaim_pending(client, [topic], group, "consumer-survivor", 0, always_fails)

    assert client.xpending(topic, group)["pending"] == 0
    dlq_entries = client.xrange("profile.changed.dlq", "-", "+")
    assert len(dlq_entries) == 1


def test_reclaim_pending_swallows_xautoclaim_errors():
    """A transient XAUTOCLAIM failure (connection blip) must not crash the
    poll loop — it just retries next pass."""
    bus = _bus()

    class BoomClient:
        def xautoclaim(self, **kwargs):
            raise RuntimeError("connection reset")

    seen = []
    bus._reclaim_pending(BoomClient(), ["t"], "g", "consumer-x", 1000, seen.append)
    assert seen == []


# ---------------------------------------------------------------------------
# _ensure_group — non-BUSYGROUP errors propagate
# ---------------------------------------------------------------------------


def test_ensure_group_reraises_non_busygroup_errors():
    import redis as redis_module

    bus = _bus()

    class BoomClient:
        def xgroup_create(self, **kwargs):
            raise redis_module.ResponseError("WRONGTYPE Operation against a wrong kind of value")

    with pytest.raises(redis_module.ResponseError):
        bus._ensure_group(BoomClient(), "t", "g")


# ---------------------------------------------------------------------------
# DLQ send failures
# ---------------------------------------------------------------------------


def test_send_to_dlq_returns_false_when_publish_fails():
    bus = _bus()
    bus.publish = lambda topic, event: (_ for _ in ()).throw(RuntimeError("dlq broker down"))
    ok = bus._send_to_dlq("t", Event(event_type="t", service="s"))
    assert ok is False


def test_send_raw_to_dlq_returns_false_when_publish_fails():
    bus = _bus()
    bus.publish = lambda topic, event: (_ for _ in ()).throw(RuntimeError("dlq broker down"))
    ok = bus._send_raw_to_dlq("t", b"raw bytes")
    assert ok is False


# ---------------------------------------------------------------------------
# _get_client — lazy connection via redis.Redis.from_url, cached afterwards
# ---------------------------------------------------------------------------


def test_get_client_lazily_connects_and_caches(monkeypatch):
    import redis as redis_module
    from stapel_core.bus.backends.redis_streams import RedisStreamsBus

    captured = {}
    fake_client = fakeredis.FakeRedis(decode_responses=False)

    def fake_from_url(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return fake_client

    monkeypatch.setattr(redis_module.Redis, "from_url", staticmethod(fake_from_url))
    monkeypatch.setenv("STAPEL_REDIS_BUS_URL", "redis://test-host:6379/5")

    bus = RedisStreamsBus()
    client = bus._get_client()

    assert client is fake_client
    assert captured["url"] == "redis://test-host:6379/5"
    assert captured["kwargs"]["decode_responses"] is False
    # Second call must not reconnect — cached instance returned directly.
    assert bus._get_client() is fake_client


def test_get_client_rechecks_cache_under_lock(monkeypatch):
    """Mirrors bus.router's own double-checked-locking coverage test: another
    thread may have populated ``_client`` between the outer None-check and
    acquiring the lock — the re-check inside must return that instance
    instead of connecting a second time."""
    from stapel_core.bus.backends.redis_streams import RedisStreamsBus

    bus = RedisStreamsBus()
    sentinel = object()

    class TrickLock:
        def __enter__(self):
            bus._client = sentinel

        def __exit__(self, *args):
            return False

    bus._client_lock = TrickLock()
    assert bus._get_client() is sentinel


# ---------------------------------------------------------------------------
# consume() — the full blocking loop (real fakeredis client, scripted stop)
# ---------------------------------------------------------------------------


def test_consume_full_loop_reads_processes_and_stops(monkeypatch):
    """Drives the actual consume() loop end to end: an empty first poll
    (exercises the ``if not response: continue`` branch), then a published
    message gets read and handled, then the loop is told to stop — all
    against a real fakeredis client, not a mocked transport."""
    import threading as real_threading

    from stapel_core.bus.backends import redis_streams as backend

    created_events = []

    class ThreadingProxy:
        def __getattr__(self, item):
            return getattr(real_threading, item)

        def Event(self):
            evt = real_threading.Event()
            created_events.append(evt)
            return evt

    monkeypatch.setattr(backend, "threading", ThreadingProxy())

    client = fakeredis.FakeRedis(decode_responses=False)
    bus = backend.RedisStreamsBus()
    bus._client = client

    topic, group = "user.deleted", "gdpr"
    event = Event(event_type=topic, service="gdpr", payload={"id": 1})

    real_xreadgroup = client.xreadgroup
    calls = {"n": 0}

    def wrapped_xreadgroup(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            client.xadd(topic, {"data": event.to_bytes()})
        result = real_xreadgroup(*args, **kwargs)
        if calls["n"] >= 2:
            created_events[0].clear()  # `running` — stop after this pass
        return result

    monkeypatch.setattr(client, "xreadgroup", wrapped_xreadgroup)

    seen = []
    bus.consume([topic], group, seen.append, poll_timeout=0.01)

    assert calls["n"] == 2  # first poll empty, second delivers + stops
    assert [e.payload for e in seen] == [{"id": 1}]
