"""An unprocessable payload is parked, not retried forever.

The class, found by a fleet audit on 2026-08-30: 27 action handlers across 12
libraries reach a queryset with an id straight from the payload.
``AUTH_USER_MODEL.id`` is a UUID, so ``filter(user_id="not-a-uuid")`` raises
``ValidationError`` inside ``UUIDField.to_python`` — and ``ValidationError`` is
not a ``ValueError``, so the ``except (ValueError, TypeError)`` guard the
consumers that *did* guard had written does not catch it.

At-least-once delivery then turns one malformed payload into an unbounded
retry loop that blocks every event behind it. Patching 27 call sites would
have left the class alive; this is the floor under all of them.
"""
from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError

from stapel_core.bus.event import Event
from stapel_core.comm.actions import deliver_to_subscribers
from stapel_core.comm.exceptions import ActionDeliveryError


def _event(name: str = "user.deleted") -> Event:
    return Event(event_type=name, service="tests", payload={"user_id": "not-a-uuid"})


def _refuses(event):
    raise ValidationError("'not-a-uuid' is not a valid UUID.")


def _breaks(event):
    raise RuntimeError("the database went away")


def test_a_validation_error_is_not_returned_for_retry():
    assert deliver_to_subscribers(_event(), [_refuses]) == []


def test_a_transient_failure_is_still_returned_for_retry():
    errors = deliver_to_subscribers(_event(), [_breaks])

    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)


def test_one_poison_handler_does_not_stop_the_others():
    seen = []

    def records(event):
        seen.append(event.event_type)

    assert deliver_to_subscribers(_event(), [_refuses, records]) == []
    assert seen == ["user.deleted"]


def test_parking_is_counted_in_the_dlq_metric(monkeypatch):
    """Silence would be indistinguishable from health — an operator must see it."""
    parked = []

    def record(topic, event=None, *, reason="handler"):
        parked.append((topic, reason, getattr(event, "event_id", None)))

    monkeypatch.setattr("stapel_core.bus.dlq.record_parked", record)

    event = _event()
    deliver_to_subscribers(event, [_refuses])

    assert parked == [("user.deleted", "unprocessable", event.event_id)]


def test_unprocessable_is_a_declared_dlq_reason():
    """declare_topics() creates the series at zero, so an alert has a subject."""
    from stapel_core.bus.dlq import REASONS

    assert "unprocessable" in REASONS


def test_a_metrics_backend_that_is_down_does_not_turn_a_park_into_a_crash(monkeypatch):
    """The park is already the failure path; it must not add a second one."""
    def explode(*args, **kwargs):
        raise RuntimeError("no metrics backend")

    monkeypatch.setattr("stapel_core.observability.metrics.counter", explode)

    assert deliver_to_subscribers(_event(), [_refuses]) == []


def test_the_raise_hatch_restores_stop_the_line(settings):
    settings.STAPEL_COMM = {"UNPROCESSABLE_PAYLOAD": "raise"}

    errors = deliver_to_subscribers(_event(), [_refuses])

    assert len(errors) == 1
    assert isinstance(errors[0], ValidationError)


def test_deliver_parks_instead_of_raising_action_delivery_error(settings):
    """The end-to-end shape: emit -> in-process delivery -> no exception."""
    from stapel_core.comm import deliver
    from stapel_core.comm.registry import action_registry

    settings.STAPEL_COMM = {"ACTION_TRANSPORT": "inprocess"}
    action_registry.clear()
    try:
        action_registry.subscribe("user.deleted", _refuses)
        deliver(_event())
    finally:
        action_registry.clear()


def test_deliver_still_raises_for_a_transient_failure(settings):
    from stapel_core.comm import deliver
    from stapel_core.comm.registry import action_registry

    settings.STAPEL_COMM = {"ACTION_TRANSPORT": "inprocess"}
    action_registry.clear()
    try:
        action_registry.subscribe("user.deleted", _breaks)
        with pytest.raises(ActionDeliveryError):
            deliver(_event())
    finally:
        action_registry.clear()


def test_the_bus_consumer_command_uses_the_same_floor(settings):
    """A bus deployment must not have different poison-pill semantics."""
    from stapel_core.comm.registry import action_registry
    from stapel_core.django.management.commands.consume_actions import Command

    action_registry.clear()
    try:
        action_registry.subscribe("user.deleted", _refuses)
        Command().handle_event(_event())

        action_registry.subscribe("user.deleted", _breaks)
        with pytest.raises(ActionDeliveryError):
            Command().handle_event(_event())
    finally:
        action_registry.clear()


# ---------------------------------------------------------------------------
# What parking costs, stated rather than left to be found
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_parked_erasure_receipts_nothing_so_the_orchestrator_still_sees_it():
    """The worst case for ack-and-drop, pinned.

    An erasure handler that parks did NOT erase. That must not read to
    stapel-gdpr as a completed section: the receipt is the only thing the
    orchestrator counts, so no receipt means the part stays open and times
    out loudly instead of the fleet believing a person's rows are gone.
    """
    from stapel_core.gdpr.owners import (
        ERASURE_REQUESTED,
        SECTION_ERASED,
        _build,
    )

    def refuses(subject_type, subject_key, workspace_id=None):
        raise ValidationError(f"'{subject_key}' is not a valid UUID.")

    registration = _build("tests", ("account",), refuses, True, "tests")
    received = []
    from stapel_core.comm.registry import action_registry

    action_registry.clear()
    try:
        action_registry.subscribe(SECTION_ERASED, received.append)
        registration.handle_erasure_requested(
            Event(
                event_type=ERASURE_REQUESTED,
                service="gdpr",
                payload={
                    "correlation_id": "c-1",
                    "subject_type": "account",
                    "subject_key": "not-a-uuid",
                },
            )
        )
    finally:
        action_registry.clear()

    assert received == [], "a refused erasure must never receipt"
