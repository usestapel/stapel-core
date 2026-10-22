"""Persistent in-process subscriptions and ``backend_for``.

A library consumer that has no process to run in on an in-process bus
subscribes with ``subscribe_in_process``. The subscription must survive
``reset_bus()`` and ``MemoryBus.clear()`` (an instance subscription did not),
reach the topic through a routing bus, and be the one mechanism core's own
producers rely on (``sync_cdn_refs``).
"""
import pytest
from django.test import override_settings

from stapel_core.bus import (
    carries_in_process,
    get_bus,
    in_process_subscribers,
    publish,
    reset_bus,
    subscribe_in_process,
    unsubscribe_in_process,
)
from stapel_core.bus.backends.memory import MemoryBus
from stapel_core.bus.backends.routing import RoutingBus
from stapel_core.bus.event import Event
from stapel_core.bus.router import SHORTHANDS

TOPIC = "tests.in-process"


class BrokerBackend:
    in_process = False

    def __init__(self) -> None:
        self.published = []

    def publish(self, topic, event):
        self.published.append((topic, event))


def _event() -> Event:
    return Event(event_type="tests.thing", service="tests", payload={"n": 1})


@pytest.fixture
def received():
    seen = []

    def handler(event):
        seen.append(event)

    subscribe_in_process(TOPIC, handler)
    yield seen
    unsubscribe_in_process(TOPIC, handler)


def _routing(routes: dict) -> RoutingBus:
    with override_settings(STAPEL_BUS_ROUTES=routes):
        return RoutingBus()


class TestPersistence:
    def test_survives_reset_bus(self, received):
        reset_bus()
        publish(TOPIC, _event())
        assert len(received) == 1

    def test_survives_clear(self, received):
        bus = get_bus()
        assert isinstance(bus, MemoryBus)
        bus.clear()
        publish(TOPIC, _event())
        assert len(received) == 1

    def test_subscribing_twice_delivers_once(self, received):
        subscribe_in_process(TOPIC, in_process_subscribers(TOPIC)[0])
        get_bus().subscribe(TOPIC, in_process_subscribers(TOPIC)[0])
        publish(TOPIC, _event())
        assert len(received) == 1

    def test_unsubscribe(self):
        def handler(event):  # pragma: no cover - must not run
            raise AssertionError

        subscribe_in_process(TOPIC, handler)
        assert unsubscribe_in_process(TOPIC, handler) is True
        assert unsubscribe_in_process(TOPIC, handler) is False
        publish(TOPIC, _event())

    def test_a_failing_handler_does_not_reach_the_publisher(self, received):
        def boom(event):
            raise RuntimeError("consumer bug")

        subscribe_in_process(TOPIC, boom)
        try:
            publish(TOPIC, _event())
        finally:
            unsubscribe_in_process(TOPIC, boom)
        assert len(received) == 1

    def test_a_broker_backend_ignores_the_registry(self, received):
        broker = BrokerBackend()
        broker.publish(TOPIC, _event())
        assert received == []


class TestRouting:
    def test_backend_for_names_the_route(self):
        bus = _routing({"": "memory"})
        assert isinstance(bus.backend_for(TOPIC), MemoryBus)
        assert bus.backend_for(TOPIC) is bus.backend_for("other.topic")

    def test_a_plain_backend_carries_its_own_topics(self):
        bus = MemoryBus()
        assert bus.backend_for(TOPIC) is bus

    def test_carries_in_process_asks_the_route(self):
        bus = _routing({"": "memory", "task.": "kafka"})
        bus._backends[SHORTHANDS["kafka"]] = BrokerBackend()
        assert carries_in_process(TOPIC, bus) is True
        assert carries_in_process("task.x", bus) is False
        assert bus.in_process is False

    def test_a_memory_route_delivers_to_the_subscription(self, received):
        bus = _routing({"": "memory"})
        bus.publish(TOPIC, _event())
        assert len(received) == 1


class TestRefSyncUsesTheSubscription:
    """``sync_cdn_refs`` relies on the subscription, never on both paths."""

    @pytest.fixture
    def cdn_function(self):
        from stapel_core.comm.registry import function_registry

        calls = []
        function_registry._providers["cdn.refs_sync"] = calls.append
        yield calls
        function_registry._providers.pop("cdn.refs_sync", None)

    def test_a_subscribed_consumer_gets_the_claim_once(self, cdn_function):
        from stapel_core.django.cdn.ref_sync import get_ref_sync_topic, sync_cdn_refs

        seen = []
        subscribe_in_process(get_ref_sync_topic(), seen.append)
        try:
            result = sync_cdn_refs("listings", "listing", 7, [], ["product/a"])
        finally:
            unsubscribe_in_process(get_ref_sync_topic(), seen.append)
        assert result.ok is True
        assert [e.payload["new_hashes"] for e in seen] == [["product/a"]]
        assert cdn_function == []

    def test_a_memory_route_without_a_subscriber_still_claims(self, cdn_function, monkeypatch):
        import stapel_core.bus as bus_pkg
        from stapel_core.bus import router
        from stapel_core.django.cdn.ref_sync import sync_cdn_refs

        bus = _routing({"": "memory"})
        monkeypatch.setattr(router, "get_bus", lambda: bus)
        monkeypatch.setattr(bus_pkg, "get_bus", lambda: bus)
        monkeypatch.setattr(bus_pkg, "publish", bus.publish)

        result = sync_cdn_refs("listings", "listing", 7, [], ["product/a"])
        assert result.ok is True
        assert [c["new_hashes"] for c in cdn_function] == [["product/a"]]
