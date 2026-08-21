import json

from stapel_core.bus import Event, get_bus, publish, reset_bus
from stapel_core.bus.backends.memory import MemoryBus


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------

class TestEvent:
    def test_defaults(self):
        e = Event(event_type="a.b", service="svc")
        assert e.version == 1
        assert e.event_id != ""
        assert e.timestamp > 0
        assert e.payload == {}
        assert e.key is None

    def test_to_json_from_json_round_trip(self):
        e = Event(event_type="foo.bar", service="test", payload={"x": 1, "y": "hello"})
        e2 = Event.from_json(e.to_json())
        assert e2.event_type == e.event_type
        assert e2.service == e.service
        assert e2.payload == e.payload
        assert e2.version == e.version
        assert e2.event_id == e.event_id

    def test_to_bytes_from_bytes_round_trip(self):
        e = Event(event_type="x", service="y", payload={"n": 42})
        e2 = Event.from_bytes(e.to_bytes())
        assert e2.event_type == "x"
        assert e2.payload == {"n": 42}

    def test_key_is_serialised(self):
        # Regression: to_json() used to pop "key" before serialising, so a
        # keyed event lost its routing key on any round trip through a
        # storage/wire boundary (the outbox: event_json = event.to_json(),
        # later re-hydrated via Event.from_json() for delivery). On Kafka,
        # KafkaBus.publish() falls back to event_id for the partition key
        # once event.key is None, so same-key events scatter across
        # partitions instead of landing on one — per-key ordering silently
        # degrades to round-robin. NATS never noticed: it partitions by
        # subject, not by this field.
        e = Event(event_type="e", service="s", key="routing-key")
        d = json.loads(e.to_json())
        assert d["key"] == "routing-key"

    def test_key_round_trips_through_json(self):
        e = Event(event_type="e", service="s", key="listing-42")
        e2 = Event.from_json(e.to_json())
        assert e2.key == "listing-42"

    def test_from_json_without_key_field_defaults_to_none(self):
        # Additive change: payloads written before this fix (or by any
        # producer that never set a key) must still deserialize cleanly.
        e2 = Event.from_json(
            json.dumps({"event_type": "a", "service": "b"})
        )
        assert e2.key is None

    def test_to_bytes_is_utf8_json(self):
        e = Event(event_type="t", service="s")
        raw = e.to_bytes()
        assert isinstance(raw, bytes)
        json.loads(raw.decode("utf-8"))  # must be valid JSON


# ---------------------------------------------------------------------------
# MemoryBus
# ---------------------------------------------------------------------------

class TestMemoryBus:
    def setup_method(self):
        self.bus = MemoryBus()

    # publish
    def test_publish_stores_in_events(self):
        e = Event(event_type="t", service="s")
        self.bus.publish("topic", e)
        assert self.bus.events == [e]

    def test_publish_multiple_events_ordered(self):
        e1 = Event(event_type="a", service="s")
        e2 = Event(event_type="b", service="s")
        self.bus.publish("t", e1)
        self.bus.publish("t", e2)
        assert self.bus.events == [e1, e2]

    # subscribe
    def test_subscribe_handler_called_on_publish(self):
        received = []
        self.bus.subscribe("my.topic", received.append)
        e = Event(event_type="x", service="s")
        self.bus.publish("my.topic", e)
        assert received == [e]

    def test_subscribe_wrong_topic_not_called(self):
        received = []
        self.bus.subscribe("other", received.append)
        self.bus.publish("my.topic", Event(event_type="x", service="s"))
        assert received == []

    def test_multiple_subscribers_same_topic(self):
        calls = []
        self.bus.subscribe("t", lambda e: calls.append(1))
        self.bus.subscribe("t", lambda e: calls.append(2))
        self.bus.publish("t", Event(event_type="e", service="s"))
        assert calls == [1, 2]

    def test_faulty_subscriber_does_not_propagate(self):
        self.bus.subscribe("t", lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
        # publish must not raise
        self.bus.publish("t", Event(event_type="e", service="s"))
        assert len(self.bus.events) == 1

    # consume (drains queue, matches by event_type)
    def test_consume_calls_handler_for_matching_event_type(self):
        received = []
        e = Event(event_type="drain.me", service="s")
        self.bus.publish("any.topic", e)
        self.bus.consume(["drain.me"], "group", received.append, poll_timeout=0.01)
        assert received == [e]

    def test_consume_skips_non_matching_event_type(self):
        received = []
        self.bus.publish("t", Event(event_type="other", service="s"))
        self.bus.consume(["drain.me"], "group", received.append, poll_timeout=0.01)
        assert received == []

    def test_consume_drains_all_matching(self):
        received = []
        for _ in range(3):
            self.bus.publish("t", Event(event_type="evt", service="s"))
        self.bus.consume(["evt"], "group", received.append, poll_timeout=0.01)
        assert len(received) == 3

    # clear
    def test_clear_resets_events(self):
        self.bus.publish("t", Event(event_type="e", service="s"))
        self.bus.clear()
        assert self.bus.events == []

    def test_clear_removes_subscribers(self):
        received = []
        self.bus.subscribe("t", received.append)
        self.bus.clear()
        self.bus.publish("t", Event(event_type="e", service="s"))
        assert received == []

    def test_clear_drains_queue(self):
        self.bus.publish("t", Event(event_type="e", service="s"))
        self.bus.clear()
        received = []
        self.bus.consume(["e"], "g", received.append, poll_timeout=0.01)
        assert received == []


# ---------------------------------------------------------------------------
# Router / singleton
# ---------------------------------------------------------------------------

class TestRouter:
    def test_get_bus_returns_memory_bus(self):
        bus = get_bus()
        assert isinstance(bus, MemoryBus)

    def test_get_bus_singleton(self):
        assert get_bus() is get_bus()

    def test_reset_bus_creates_new_instance(self):
        b1 = get_bus()
        reset_bus()
        b2 = get_bus()
        assert b1 is not b2


# ---------------------------------------------------------------------------
# publish() top-level helper
# ---------------------------------------------------------------------------

class TestPublishHelper:
    def test_publish_goes_to_singleton_bus(self):
        bus = get_bus()
        e = Event(event_type="pub.test", service="s")
        publish("topic", e)
        assert bus.events[-1] is e

    def test_publish_subscriber_called(self):
        bus = get_bus()
        received = []
        bus.subscribe("t", received.append)
        e = Event(event_type="x", service="s")
        publish("t", e)
        assert received == [e]


# ---------------------------------------------------------------------------
# KafkaBus.publish() partition key — the outbox round-trip end to end
# ---------------------------------------------------------------------------

class _FakeKafkaProducer:
    def __init__(self):
        self.produced: list[dict] = []

    def produce(self, topic, key=None, value=None, callback=None):
        self.produced.append({"topic": topic, "key": key, "value": value})

    def poll(self, timeout):
        pass


class TestKafkaBusPartitionKey:
    def test_publish_uses_event_key_for_the_partition(self):
        from stapel_core.bus.backends.kafka import KafkaBus

        bus = KafkaBus()
        fake = _FakeKafkaProducer()
        bus._producer = fake

        event = Event(event_type="listing.published", service="s", key="listing-42")
        bus.publish("listing.published", event)

        assert fake.produced[0]["key"] == b"listing-42"

    def test_outbox_round_trip_preserves_the_partition_key(self):
        """The exact defect: an event serialized for the outbox (event_json
        = event.to_json()) and later re-hydrated for delivery
        (Event.from_json(row.event_json)) must still carry its key all the
        way to the Kafka producer — otherwise KafkaBus.publish() falls back
        to the random event_id and same-key events land on different
        partitions, breaking per-key ordering."""
        from stapel_core.bus.backends.kafka import KafkaBus

        original = Event(event_type="listing.published", service="s", key="listing-42")

        # Stand-in for OutboxEvent.event_json + the relay's Event.from_json().
        stored_json = original.to_json()
        rehydrated = Event.from_json(stored_json)

        bus = KafkaBus()
        fake = _FakeKafkaProducer()
        bus._producer = fake
        bus.publish("listing.published", rehydrated)

        assert fake.produced[0]["key"] == b"listing-42"
        assert fake.produced[0]["key"] != original.event_id.encode("utf-8")
