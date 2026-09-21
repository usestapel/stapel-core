"""An announcement for a row this service does not have.

Services share one bus and separate databases. When a recordings service
starts ``llm.transcribe`` in ITS taskstore, every service that registers
that kind hears ``task.requested`` — including the agent, whose database
has never held the row. Measured on a client fleet: 32 ERROR lines a week,
one per transcription, all about tasks that were being executed fine by
the service that announced them.

The announcer's service is on the event. A miss on a FOREIGN announcement
is the topology and is skipped at INFO; a miss on this service's OWN
announcement is the anomaly the old error was written for, and it is now
also parked in the DLQ series a deployment alerts on.

These tests do not claim anything about commit ordering, so pytest-django's
transaction wrapping is irrelevant to them: the row is absent in every
isolation level, and what is asserted is the log level and the DLQ record.
"""
import logging
import uuid

import pytest

from stapel_core.bus.event import Event
from stapel_core.comm.tasks import (
    TASK_REQUESTED,
    clear_handlers,
    handle_task_requested,
    register_task,
)

KIND = "llm.transcribe"
# Spelled out rather than imported: a run against a tasks.py without the
# constant must fail on the BEHAVIOUR, not on an ImportError at collection.
REASON_ORPHAN = "orphan"


@pytest.fixture(autouse=True)
def _handler():
    clear_handlers()
    register_task(KIND, lambda payload: {"ran": True})
    yield
    clear_handlers()


@pytest.fixture
def own_service(settings):
    settings.STAPEL_COMM = {**getattr(settings, "STAPEL_COMM", {}), "SERVICE": "iron-agent"}
    return "iron-agent"


@pytest.fixture
def parked(monkeypatch):
    calls = []

    def record(topic, event=None, *, reason="handler"):
        calls.append((topic, reason))

    from stapel_core.bus import dlq

    monkeypatch.setattr(dlq, "record_parked", record)
    return calls


def _announce(service: str) -> str:
    task_id = str(uuid.uuid4())
    handle_task_requested(
        Event(event_type=TASK_REQUESTED, service=service,
              payload={"task_id": task_id, "kind": KIND})
    )
    return task_id


@pytest.mark.django_db
def test_another_services_announcement_is_not_an_error_here(own_service, parked, caplog):
    with caplog.at_level(logging.INFO, logger="stapel_core.comm.tasks"):
        task_id = _announce("iron-recordings")

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == [], [r.getMessage() for r in errors]
    assert parked == []
    skipped = [r for r in caplog.records if task_id in r.getMessage()]
    assert skipped and skipped[0].levelno == logging.INFO
    assert "iron-recordings" in skipped[0].getMessage()


@pytest.mark.django_db
def test_this_services_own_announcement_with_no_row_is_parked(own_service, parked, caplog):
    with caplog.at_level(logging.ERROR, logger="stapel_core.comm.tasks"):
        task_id = _announce(own_service)

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert task_id in errors[0].getMessage()
    assert "no such row exists" in errors[0].getMessage()
    assert parked == [(f"task.{KIND}", REASON_ORPHAN)]


@pytest.mark.django_db
def test_an_unattributed_announcement_is_treated_as_our_own(own_service, parked, caplog):
    """No service on the event: nothing says it is somebody else's, so it
    keeps the loud path."""
    with caplog.at_level(logging.ERROR, logger="stapel_core.comm.tasks"):
        _announce("")

    assert [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert parked == [(f"task.{KIND}", REASON_ORPHAN)]


@pytest.mark.django_db
def test_a_service_with_no_name_cannot_tell_and_stays_loud(settings, parked, caplog):
    settings.STAPEL_COMM = {
        k: v for k, v in getattr(settings, "STAPEL_COMM", {}).items() if k != "SERVICE"
    }
    settings.SERVICE_NAME = ""
    with caplog.at_level(logging.ERROR, logger="stapel_core.comm.tasks"):
        _announce("iron-recordings")

    assert [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert parked == [(f"task.{KIND}", REASON_ORPHAN)]
