"""Tests for the NATS JetStream bus backend and env-first backend selection."""
import pytest
from django.test import override_settings

from stapel_core.bus.event import Event
from stapel_core.bus.router import SHORTHANDS, _resolve_backend_path, get_bus, reset_bus


# ---------------------------------------------------------------------------
# Backend selection: env > setting > default; shorthands
# ---------------------------------------------------------------------------


def test_shorthands_cover_all_three_backends():
    assert set(SHORTHANDS) == {"memory", "kafka", "nats"}


def test_env_wins_over_setting(monkeypatch):
    monkeypatch.setenv("STAPEL_BUS_BACKEND", "nats")
    with override_settings(STAPEL_BUS_BACKEND="memory"):
        assert _resolve_backend_path() == SHORTHANDS["nats"]


def test_setting_used_when_env_absent(monkeypatch):
    monkeypatch.delenv("STAPEL_BUS_BACKEND", raising=False)
    with override_settings(STAPEL_BUS_BACKEND="memory"):
        assert _resolve_backend_path() == SHORTHANDS["memory"]


def test_dotted_path_passthrough(monkeypatch):
    monkeypatch.setenv("STAPEL_BUS_BACKEND", "my_app.bus.CustomBus")
    assert _resolve_backend_path() == "my_app.bus.CustomBus"


def test_get_bus_instantiates_env_backend(monkeypatch):
    monkeypatch.setenv("STAPEL_BUS_BACKEND", "memory")
    reset_bus()
    try:
        from stapel_core.bus.backends.memory import MemoryBus

        assert isinstance(get_bus(), MemoryBus)
    finally:
        reset_bus()


# ---------------------------------------------------------------------------
# Subject mapping
# ---------------------------------------------------------------------------


def test_subject_mapping_default_and_overridden(monkeypatch):
    from stapel_core.bus._config import NatsBusConfig

    monkeypatch.delenv("STAPEL_NATS_EVENT_PREFIX", raising=False)
    assert NatsBusConfig.subject_for("user.deleted") == "stapel.evt.user.deleted"

    monkeypatch.setenv("STAPEL_NATS_EVENT_PREFIX", "acme.events")
    assert NatsBusConfig.subject_for("user.deleted") == "acme.events.user.deleted"


def test_env_config_beats_setting(monkeypatch):
    from stapel_core.bus._config import NatsBusConfig

    monkeypatch.setenv("NATS_URL", "nats://from-env:4222")
    with override_settings(NATS_URL="nats://from-settings:4222"):
        assert NatsBusConfig.url() == "nats://from-env:4222"


def test_durable_name_sanitized():
    from stapel_core.bus.backends.nats import _durable_name

    assert _durable_name("iron.notifications.contacts") == "iron_notifications_contacts"
    assert _durable_name("") == "stapel"


# ---------------------------------------------------------------------------
# Message processing: ack / retry / DLQ semantics (no server needed)
# ---------------------------------------------------------------------------


@pytest.fixture()
def fast_retries(monkeypatch):
    from stapel_core.bus.backends import nats as nats_backend

    monkeypatch.setattr(nats_backend.time, "sleep", lambda s: None)
    return nats_backend


def _bus():
    from stapel_core.bus.backends.nats import NatsJetStreamBus

    return NatsJetStreamBus()


@pytest.mark.django_db
def test_process_success_returns_none(fast_retries):
    seen = []
    event = Event(event_type="user.deleted", service="gdpr", payload={"user_id": "u1"})
    outcome = _bus()._process(event.to_bytes(), lambda e: seen.append(e))
    assert outcome is None
    assert seen[0].payload == {"user_id": "u1"}


@pytest.mark.django_db
def test_process_retries_then_succeeds(fast_retries):
    calls = {"n": 0}

    def flaky(event):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")

    event = Event(event_type="payment.completed", service="billing", payload={})
    assert _bus()._process(event.to_bytes(), flaky) is None
    assert calls["n"] == 3


@pytest.mark.django_db
def test_process_exhausted_retries_goes_to_dlq(fast_retries):
    def always_fails(event):
        raise RuntimeError("permanent")

    event = Event(event_type="payment.completed", service="billing", payload={})
    outcome = _bus()._process(event.to_bytes(), always_fails)
    assert outcome is not None
    dlq_subject, payload = outcome
    assert dlq_subject == "stapel.evt.payment.completed.dlq"
    assert Event.from_bytes(payload).event_id == event.event_id


@pytest.mark.django_db
def test_process_poison_message_goes_to_dlq(fast_retries):
    outcome = _bus()._process(b"\xff not json", lambda e: None)
    assert outcome is not None
    dlq_subject, payload = outcome
    assert dlq_subject == "stapel.evt.__undecodable__.dlq"
    wrapper = Event.from_bytes(payload)
    assert wrapper.event_type == "__undecodable__"


def test_dlq_subject_helper():
    from stapel_core.bus.backends.nats import dlq_subject_for

    assert dlq_subject_for("user.deleted") == "stapel.evt.user.deleted.dlq"
