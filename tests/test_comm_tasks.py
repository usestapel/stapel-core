"""Tests for the comm Task primitive (async named operations)."""
import pytest
from django.db import transaction

from stapel_core.bus.event import Event
from stapel_core.comm import start, status
from stapel_core.comm.registry import action_registry
from stapel_core.comm.tasks import (
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_REQUESTED,
    TaskNotFound,
    clear_handlers,
    execute,
    handle_task_requested,
    register_task,
)
from stapel_core.django.taskstore.models import TaskRecord


_emitted = []


@pytest.fixture(autouse=True)
def clean():
    """Reset registries, re-wire the framework subscriber, capture outcome
    events. One fixture — autouse ordering between separate fixtures is not
    guaranteed, and a clear() running after the capture subscription would
    silently drop it."""
    from stapel_core.comm.actions import subscribe_action

    clear_handlers()
    action_registry.clear()
    _emitted.clear()
    # Re-wire what the taskstore app's ready() registers.
    subscribe_action(TASK_REQUESTED, handle_task_requested)
    subscribe_action(TASK_COMPLETED, _emitted.append)
    subscribe_action(TASK_FAILED, _emitted.append)
    yield
    clear_handlers()
    action_registry.clear()
    _emitted.clear()


@pytest.mark.django_db(transaction=True)
def test_start_executes_after_commit_and_stores_result():
    register_task("math.double", lambda p: {"value": p["n"] * 2})

    with transaction.atomic():
        task_id = start("math.double", {"n": 21})
        # nothing ran yet — the transaction is open
        assert status(task_id).state == TaskRecord.PENDING

    st = status(task_id)
    assert st.state == TaskRecord.DONE
    assert st.result == {"value": 42}
    assert st.attempts == 1
    assert [e.event_type for e in _emitted] == [TASK_COMPLETED]
    assert _emitted[0].payload["task_id"] == task_id


@pytest.mark.django_db(transaction=True)
def test_rollback_discards_task():
    register_task("math.double", lambda p: p)

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with transaction.atomic():
            start("math.double", {"n": 1})
            raise Boom()

    assert TaskRecord.objects.count() == 0
    assert _emitted == []


@pytest.mark.django_db(transaction=True)
def test_retry_then_success():
    calls = {"n": 0}

    def flaky(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return {"ok": True}

    register_task("flaky.op", flaky)
    with transaction.atomic():
        task_id = start("flaky.op", max_attempts=3)

    st = status(task_id)
    assert st.state == TaskRecord.DONE
    assert st.attempts == 2
    assert calls["n"] == 2


@pytest.mark.django_db(transaction=True)
def test_exhausted_attempts_fail_with_event():
    def broken(payload):
        raise RuntimeError("permanent")

    register_task("broken.op", broken)
    with transaction.atomic():
        task_id = start("broken.op", max_attempts=2)

    st = status(task_id)
    assert st.state == TaskRecord.FAILED
    assert "permanent" in st.error
    assert st.attempts == 2
    assert _emitted[-1].event_type == TASK_FAILED


@pytest.mark.django_db(transaction=True)
def test_foreign_kind_ignored():
    """A requested-event for a kind owned by another service is skipped."""
    record = TaskRecord.objects.create(kind="other.service.op", payload={})
    handle_task_requested(
        Event(event_type=TASK_REQUESTED, service="x",
              payload={"task_id": str(record.pk), "kind": "other.service.op"})
    )
    record.refresh_from_db()
    assert record.state == TaskRecord.PENDING


@pytest.mark.django_db(transaction=True)
def test_execute_is_idempotent_on_redelivery():
    calls = {"n": 0}

    def once(payload):
        calls["n"] += 1
        return {}

    register_task("once.op", once)
    with transaction.atomic():
        task_id = start("once.op")
    # redelivered requested-event
    execute(task_id)
    execute(task_id)
    assert calls["n"] == 1


@pytest.mark.django_db(transaction=True)
def test_callback_function_invoked():
    received = []
    from stapel_core.comm import register_function

    register_function("notify.done", lambda p: received.append(p) or {"ok": True})
    register_task("cb.op", lambda p: {"answer": 7})

    with transaction.atomic():
        task_id = start("cb.op", callback="notify.done")

    assert received and received[0]["task_id"] == task_id
    assert received[0]["state"] == TaskRecord.DONE
    assert received[0]["result"] == {"answer": 7}


@pytest.mark.django_db(transaction=True)
def test_sweep_fails_expired_tasks():
    from django.core.management import call_command
    from django.utils import timezone

    record = TaskRecord.objects.create(
        kind="slow.op", deadline=timezone.now(), state=TaskRecord.RUNNING
    )
    call_command("sweep_tasks")
    record.refresh_from_db()
    assert record.state == TaskRecord.FAILED
    assert record.error == "deadline exceeded"
    assert _emitted[-1].event_type == TASK_FAILED


@pytest.mark.django_db
def test_status_unknown_id_raises():
    with pytest.raises(TaskNotFound):
        status("00000000-0000-0000-0000-000000000000")


@pytest.mark.django_db(transaction=True)
def test_task_completed_subscriber_pattern():
    """The documented consumption pattern: subscribe and filter by kind."""
    got = []

    from stapel_core.comm import on_action

    @on_action(TASK_COMPLETED)
    def on_done(event):
        if event.payload["kind"] == "llm.summarize":
            got.append(event.payload["task_id"])

    register_task("llm.summarize", lambda p: {"summary": "..."})
    with transaction.atomic():
        task_id = start("llm.summarize", {"doc": 1}, correlation_id="doc-1")

    assert got == [task_id]
