"""A park is announced in process, not only counted.

``bus_dlq_total`` says how much work is being dropped; it cannot say WHAT was
dropped, because an event id and a traceback are unbounded as label values.
Until this signal existed the only way to learn that was to scrape the log
line — which is how the 2026-09-13 client-fleet investigation actually went.
"""
import sys

import pytest
from django.dispatch import receiver

from stapel_core.bus.dlq import record_parked
from stapel_core.signals import bus_event_parked


class _Event:
    event_type = "workspace.personal.created"
    event_id = "005b0408-fe17-4b0a-b159-90511fe5cb5d"


@pytest.fixture
def heard():
    seen = []

    @receiver(bus_event_parked)
    def _listen(sender, **kwargs):
        seen.append(kwargs)

    try:
        yield seen
    finally:
        bus_event_parked.disconnect(_listen)


def test_a_park_reaches_a_listener_with_topic_event_and_reason(heard):
    event = _Event()
    record_parked("workspace.personal.created", event, reason="handler")

    assert len(heard) == 1
    assert heard[0]["topic"] == "workspace.personal.created"
    assert heard[0]["event"] is event
    assert heard[0]["reason"] == "handler"


def test_the_traceback_travels_when_the_park_is_inside_an_except_block(heard):
    try:
        raise ValueError("foreign key violation")
    except ValueError:
        record_parked("task.transcribe", None, reason="unprocessable")

    exc_info = heard[0]["exc_info"]
    assert exc_info is not None
    assert exc_info[0] is ValueError
    assert str(exc_info[1]) == "foreign key violation"


def test_no_exception_in_flight_means_no_exc_info(heard):
    record_parked("a.topic", None, reason="undecodable")

    assert heard[0]["exc_info"] is None
    assert sys.exc_info()[0] is None


def test_a_listener_that_raises_does_not_break_the_park(heard):
    @receiver(bus_event_parked)
    def _explode(sender, **kwargs):
        raise RuntimeError("the alert store is down")

    try:
        record_parked("a.topic", _Event(), reason="handler")
    finally:
        bus_event_parked.disconnect(_explode)

    # The park still happened, and the healthy listener still heard it.
    assert len(heard) == 1
