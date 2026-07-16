"""Tests for stapel_core.bus.checks and the loud publish() error logging.

Covers the two owner-visible failure modes from the kafka-default incident
(request_notification's OTP emails never left the process because
publish() raised ModuleNotFoundError deep inside KafkaBus and the caller
fail-softs on publish errors by contract):

1. ``manage.py check`` (system checks) fails loudly when STAPEL_BUS_BACKEND
   names a transport whose client library is not installed.
2. ``bus.publish()`` itself logs an error-level, fix-it message before
   re-raising — so even a caller that fail-softs leaves a loud trace.
"""
from __future__ import annotations

import logging
import sys

import pytest

from stapel_core.bus import Event, publish, reset_bus
from stapel_core.bus.checks import (
    E001_MISSING_TRANSPORT_LIBRARY,
    check_bus_backend_library,
)


@pytest.fixture
def poisoned_module():
    """Make ``import <name>`` raise ImportError for the duration of a test,
    restoring whatever was there before (the real package, if installed)."""
    poisoned = []

    def _poison(name: str):
        saved = sys.modules.get(name)
        sys.modules[name] = None
        poisoned.append((name, saved))

    yield _poison

    for name, saved in poisoned:
        sys.modules.pop(name, None)
        if saved is not None:
            sys.modules[name] = saved


# ---------------------------------------------------------------------------
# System check
# ---------------------------------------------------------------------------


def test_check_clean_on_memory_backend(settings):
    settings.STAPEL_BUS_BACKEND = "memory"
    assert check_bus_backend_library() == []


def test_check_clean_on_routing_shorthand(settings):
    # "routing" itself needs no third-party client; per-topic backends (if
    # also named directly) are checked on their own merits.
    settings.STAPEL_BUS_BACKEND = "routing"
    assert check_bus_backend_library() == []


def test_check_clean_on_custom_dotted_path(settings):
    settings.STAPEL_BUS_BACKEND = "my_app.bus.CustomBus"
    assert check_bus_backend_library() == []


def test_check_errors_when_kafka_configured_but_confluent_kafka_missing(
    settings, poisoned_module
):
    settings.STAPEL_BUS_BACKEND = "kafka"
    poisoned_module("confluent_kafka")

    errors = check_bus_backend_library()

    assert len(errors) == 1
    assert errors[0].id == E001_MISSING_TRANSPORT_LIBRARY
    assert "kafka" in errors[0].msg
    assert "confluent_kafka" in errors[0].msg
    assert "stapel-core[kafka]" in errors[0].hint


def test_check_errors_when_nats_configured_but_nats_py_missing(
    settings, poisoned_module
):
    settings.STAPEL_BUS_BACKEND = "nats"
    poisoned_module("nats")

    errors = check_bus_backend_library()

    assert len(errors) == 1
    assert errors[0].id == E001_MISSING_TRANSPORT_LIBRARY
    assert "stapel-core[nats]" in errors[0].hint


def test_check_clean_when_kafka_configured_and_confluent_kafka_installed(settings):
    pytest.importorskip("confluent_kafka")
    settings.STAPEL_BUS_BACKEND = "kafka"
    assert check_bus_backend_library() == []


def test_check_env_wins_over_setting(settings, monkeypatch, poisoned_module):
    """The check must resolve the backend the same way get_bus() does:
    env > setting > default — otherwise it could bless a setting that isn't
    actually in effect."""
    settings.STAPEL_BUS_BACKEND = "memory"
    monkeypatch.setenv("STAPEL_BUS_BACKEND", "kafka")
    poisoned_module("confluent_kafka")

    errors = check_bus_backend_library()
    assert len(errors) == 1
    assert errors[0].id == E001_MISSING_TRANSPORT_LIBRARY


# ---------------------------------------------------------------------------
# Loud error logging on publish()
# ---------------------------------------------------------------------------


def test_publish_succeeds_quietly_on_memory_backend(settings, caplog):
    settings.STAPEL_BUS_BACKEND = "memory"
    reset_bus()
    try:
        with caplog.at_level(logging.ERROR, logger="stapel_core.bus"):
            publish("test.topic", Event(event_type="test.topic", service="s"))
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    finally:
        reset_bus()


def test_publish_logs_error_and_reraises_when_transport_missing(
    settings, poisoned_module, caplog
):
    settings.STAPEL_BUS_BACKEND = "kafka"
    poisoned_module("confluent_kafka")
    reset_bus()
    try:
        with caplog.at_level(logging.ERROR, logger="stapel_core.bus"):
            with pytest.raises(ImportError):
                publish("notification.requested", Event(
                    event_type="notification.requested", service="s",
                ))
        error_logs = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(error_logs) == 1
        message = error_logs[0].getMessage()
        assert "notification.requested" in message
        assert "stapel-core[kafka]" in message
    finally:
        reset_bus()


def test_publish_does_not_swallow_non_import_errors(settings):
    """A backend that fails for a reason *other* than a missing transport
    library must still propagate — publish() only adds logging, never a
    fail-open default."""

    class BoomBus:
        def publish(self, topic, event):
            raise RuntimeError("broker unreachable")

        def consume(self, *a, **k):
            raise NotImplementedError

    from stapel_core.bus import router

    reset_bus()
    router._bus = BoomBus()
    try:
        with pytest.raises(RuntimeError, match="broker unreachable"):
            publish("test.topic", Event(event_type="test.topic", service="s"))
    finally:
        reset_bus()
