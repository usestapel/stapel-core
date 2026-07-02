"""RoutingBus — per-topic-prefix broker selection (STAPEL_BUS_ROUTES)."""
import pytest
from django.test import override_settings

from stapel_core.bus import get_bus, publish, reset_bus
from stapel_core.bus.backends.memory import MemoryBus
from stapel_core.bus.backends.routing import RoutingBus
from stapel_core.bus.event import Event
from stapel_core.bus.router import SHORTHANDS

KAFKA = SHORTHANDS["kafka"]
NATS = SHORTHANDS["nats"]
MEMORY = SHORTHANDS["memory"]


class FakeBackend:
    """Records calls; stands in for real broker backends in the cache."""

    def __init__(self) -> None:
        self.published: list[tuple[str, Event]] = []
        self.consumed: tuple | None = None

    def publish(self, topic, event):
        self.published.append((topic, event))

    def consume(self, topics, group, handler, *, poll_timeout=0.1):
        self.consumed = (list(topics), group, handler)


def _event(name: str = "x") -> Event:
    return Event(event_type=name, service="tests", payload={})


def _routing(routes: dict, fakes: dict | None = None) -> RoutingBus:
    with override_settings(STAPEL_BUS_ROUTES=routes):
        bus = RoutingBus()
    for dotted, fake in (fakes or {}).items():
        bus._backends[dotted] = fake
    return bus


class TestConfig:
    def test_shorthand_registered(self):
        assert SHORTHANDS["routing"] == "stapel_core.bus.backends.routing.RoutingBus"

    def test_get_bus_resolves_routing_shorthand(self, monkeypatch):
        monkeypatch.setenv("STAPEL_BUS_BACKEND", "routing")
        monkeypatch.setenv("STAPEL_BUS_ROUTES", '{"": "memory"}')
        reset_bus()
        assert isinstance(get_bus(), RoutingBus)

    def test_env_json_parsed(self, monkeypatch):
        monkeypatch.setenv("STAPEL_BUS_ROUTES", '{"task.": "kafka", "": "nats"}')
        assert RoutingBus()._routes == {"task.": "kafka", "": "nats"}

    def test_env_wins_over_setting(self, monkeypatch):
        monkeypatch.setenv("STAPEL_BUS_ROUTES", '{"": "memory"}')
        with override_settings(STAPEL_BUS_ROUTES={"": "kafka"}):
            assert RoutingBus()._routes == {"": "memory"}

    def test_setting_dict_used_without_env(self):
        with override_settings(STAPEL_BUS_ROUTES={"task.": "kafka", "": "nats"}):
            assert RoutingBus()._routes == {"task.": "kafka", "": "nats"}

    def test_invalid_env_json_rejected(self, monkeypatch):
        monkeypatch.setenv("STAPEL_BUS_ROUTES", "{not json")
        with pytest.raises(ValueError, match="not valid JSON"):
            RoutingBus()

    def test_missing_routes_rejected(self):
        with pytest.raises(ValueError, match="STAPEL_BUS_ROUTES"):
            RoutingBus()

    def test_non_dict_routes_rejected(self, monkeypatch):
        monkeypatch.setenv("STAPEL_BUS_ROUTES", '["task."]')
        with pytest.raises(ValueError, match="must be a dict"):
            RoutingBus()


class TestPublish:
    def test_longest_prefix_wins(self):
        kafka, nats = FakeBackend(), FakeBackend()
        bus = _routing(
            {"task.": "kafka", "": "nats"}, {KAFKA: kafka, NATS: nats}
        )
        bus.publish("task.requested", _event("task.requested"))
        bus.publish("user.deleted", _event("user.deleted"))
        bus.publish("task", _event("task"))  # no dot — not the task. prefix
        assert [t for t, _ in kafka.published] == ["task.requested"]
        assert [t for t, _ in nats.published] == ["user.deleted", "task"]

    def test_longer_prefix_beats_shorter(self):
        kafka, nats, memory = FakeBackend(), FakeBackend(), FakeBackend()
        bus = _routing(
            {"task.": "kafka", "task.export.": "memory", "": "nats"},
            {KAFKA: kafka, NATS: nats, MEMORY: memory},
        )
        bus.publish("task.export.csv", _event())
        assert [t for t, _ in memory.published] == ["task.export.csv"]
        assert kafka.published == []

    def test_no_matching_route_rejected(self):
        bus = _routing({"task.": "memory"})
        with pytest.raises(ValueError, match='empty prefix ""'):
            bus.publish("user.deleted", _event())

    def test_route_to_routing_itself_rejected(self):
        bus = _routing({"": "routing"})
        with pytest.raises(ValueError, match="concrete backends"):
            bus.publish("user.deleted", _event())

    def test_one_backend_instance_per_distinct_target(self):
        bus = _routing({"task.": "memory", "": "memory"})
        first = bus._backend_for(bus._target_for("task.requested"))
        second = bus._backend_for(bus._target_for("user.deleted"))
        assert first is second
        assert isinstance(first, MemoryBus)

    def test_end_to_end_through_module_publish(self, monkeypatch):
        monkeypatch.setenv("STAPEL_BUS_BACKEND", "routing")
        monkeypatch.setenv("STAPEL_BUS_ROUTES", '{"": "memory"}')
        reset_bus()
        publish("profile.changed", _event("profile.changed"))
        inner = get_bus()._backends[MEMORY]
        assert [e.event_type for e in inner.events] == ["profile.changed"]


class TestConsume:
    def test_single_backend_delegation(self):
        kafka = FakeBackend()
        bus = _routing({"task.": "kafka", "": "nats"}, {KAFKA: kafka})
        handler = lambda e: None  # noqa: E731
        bus.consume(["task.requested", "task.completed"], "workers", handler)
        assert kafka.consumed == (["task.requested", "task.completed"], "workers", handler)

    def test_mixed_backends_rejected(self):
        bus = _routing({"task.": "kafka", "": "nats"})
        with pytest.raises(ValueError, match="split the consumer"):
            bus.consume(["task.requested", "user.deleted"], "g", lambda e: None)
