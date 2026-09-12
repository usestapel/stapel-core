"""Tests for the comm Task primitive (async named operations)."""
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone

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
def test_retry_then_success(settings):
    """With the ladder disabled, a transient failure retries in the same
    call chain — the pre-0.60 behaviour, now something a deployment ASKS
    for rather than the only thing on offer."""
    settings.STAPEL_COMM = {
        **getattr(settings, "STAPEL_COMM", {}), "TASK_RETRY_BACKOFF_BASE": 0,
    }
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
def test_retry_is_held_for_the_backoff_and_not_hammered(settings):
    """THE regression this ladder exists for.

    Measured on a client fleet's stand before it: 215 parked screening
    tasks, every one at attempts=3, mean lifetime 0.87 SECONDS — three
    provider calls fired inside one second because the requeue
    re-announced instantly. With a ladder, the second attempt does not
    happen until its hold expires; the provider gets one call, not three.
    """
    settings.STAPEL_COMM = {
        **getattr(settings, "STAPEL_COMM", {}), "TASK_RETRY_BACKOFF_BASE": 60,
    }
    calls = {"n": 0}

    def flaky(payload):
        calls["n"] += 1
        raise RuntimeError("provider unreachable")

    register_task("held.op", flaky)
    with transaction.atomic():
        task_id = start("held.op", max_attempts=3)

    st = status(task_id)
    # One call, not three. The row is waiting, not given up on.
    assert calls["n"] == 1
    assert st.state == TaskRecord.PENDING
    assert st.attempts == 1

    record = TaskRecord.objects.get(pk=task_id)
    assert record.not_before is not None
    assert record.not_before > timezone.now()

    # A redelivered announcement arriving inside the hold is declined —
    # it must not spend the attempt it is not yet entitled to.
    execute(task_id)
    assert calls["n"] == 1
    assert status(task_id).attempts == 1


@pytest.mark.django_db(transaction=True)
def test_due_retry_runs_when_the_hold_expires(settings):
    settings.STAPEL_COMM = {
        **getattr(settings, "STAPEL_COMM", {}), "TASK_RETRY_BACKOFF_BASE": 60,
    }
    calls = {"n": 0}

    def flaky(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return {"ok": True}

    register_task("due.op", flaky)
    with transaction.atomic():
        task_id = start("due.op", max_attempts=3)
    assert status(task_id).state == TaskRecord.PENDING

    # Wind the hold back rather than sleeping for it.
    TaskRecord.objects.filter(pk=task_id).update(
        not_before=timezone.now() - timedelta(seconds=1)
    )
    execute(task_id)

    assert status(task_id).state == TaskRecord.DONE
    assert calls["n"] == 2


@pytest.mark.django_db(transaction=True)
def test_exhausted_attempts_fail_with_named_reason(settings):
    settings.STAPEL_COMM = {
        **getattr(settings, "STAPEL_COMM", {}), "TASK_RETRY_BACKOFF_BASE": 0,
    }

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
    # The reason is on the row and in the event — an operator groups on it
    # without LIKE-matching a repr().
    assert _emitted[-1].payload["reason"] == TaskRecord.REASON_HANDLER
    assert TaskRecord.objects.get(pk=task_id).failure_reason == (
        TaskRecord.REASON_HANDLER
    )


@pytest.mark.django_db(transaction=True)
def test_unprocessable_payload_is_parked_on_the_first_refusal(settings):
    """A handler that refuses the VALUES is never retried.

    Retrying reproduces the refusal exactly, and on a priced surface bills
    for every reproduction. Actions have parked these since 0.53; the
    primitive that actually calls the paid provider did not.
    """
    settings.STAPEL_COMM = {
        **getattr(settings, "STAPEL_COMM", {}), "TASK_RETRY_BACKOFF_BASE": 0,
    }
    calls = {"n": 0}

    def refuses(payload):
        calls["n"] += 1
        raise DjangoValidationError("listing_id is not a uuid")

    register_task("poison.op", refuses)
    with transaction.atomic():
        task_id = start("poison.op", max_attempts=5)

    st = status(task_id)
    assert st.state == TaskRecord.FAILED
    assert st.attempts == 1, "a poison payload must not spend its whole ladder"
    assert calls["n"] == 1
    assert TaskRecord.objects.get(pk=task_id).failure_reason == (
        TaskRecord.REASON_UNPROCESSABLE
    )


@pytest.mark.django_db(transaction=True)
def test_dedupe_key_returns_the_live_task_instead_of_a_second_one():
    """Idempotency: the retried publish costs one provider call, not two."""
    runs = {"n": 0}
    register_task("vision.draft", lambda p: runs.__setitem__("n", runs["n"] + 1))

    with transaction.atomic():
        first = start("vision.draft", {"a": 1}, dedupe_key="listing-42")
    # The task ran and is DONE, so the key is released — a completed task
    # does not block the next one.
    with transaction.atomic():
        second = start("vision.draft", {"a": 1}, dedupe_key="listing-42")
    assert second != first

    # ...but while one is still PENDING, a second start() joins it.
    TaskRecord.objects.filter(pk=second).update(state=TaskRecord.PENDING)
    with transaction.atomic():
        third = start("vision.draft", {"a": 1}, dedupe_key="listing-42")
    assert third == second
    assert TaskRecord.objects.filter(dedupe_key="listing-42").count() == 2


@pytest.mark.django_db(transaction=True)
def test_dedupe_key_empty_never_collides():
    register_task("plain.op", lambda p: {"ok": True})
    with transaction.atomic():
        a = start("plain.op")
    with transaction.atomic():
        b = start("plain.op")
    assert a != b


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
    assert record.failure_reason == TaskRecord.REASON_DEADLINE
    assert _emitted[-1].event_type == TASK_FAILED
    assert _emitted[-1].payload["reason"] == TaskRecord.REASON_DEADLINE


@pytest.mark.django_db(transaction=True)
def test_sweep_reannounces_a_due_retry_whose_announcement_was_lost():
    """The property that makes the ladder durable rather than hopeful.

    A retry re-announces through the outbox, and that announcement can
    still be lost — a consumer crash between relay and claim, or a
    redelivery that arrived during the hold and was correctly declined.
    Without this sweep the row sits PENDING with an expired hold and
    nothing ever looks at it again: a task that reports "retrying" forever
    and never retries.
    """
    from django.core.management import call_command

    calls = {"n": 0}
    register_task("resumed.op", lambda p: calls.__setitem__("n", calls["n"] + 1))

    # A task mid-ladder whose hold has expired and whose announcement never
    # arrived.
    record = TaskRecord.objects.create(
        kind="resumed.op",
        state=TaskRecord.PENDING,
        attempts=1,
        max_attempts=3,
        not_before=timezone.now() - timedelta(seconds=5),
    )

    call_command("sweep_tasks")

    record.refresh_from_db()
    assert record.state == TaskRecord.DONE
    assert calls["n"] == 1


@pytest.mark.django_db(transaction=True)
def test_sweep_leaves_a_held_retry_alone():
    """A hold that has NOT expired is not woken early — otherwise the sweep
    becomes the retry storm it was added to prevent."""
    from django.core.management import call_command

    calls = {"n": 0}
    register_task("waiting.op", lambda p: calls.__setitem__("n", calls["n"] + 1))
    TaskRecord.objects.create(
        kind="waiting.op",
        state=TaskRecord.PENDING,
        attempts=1,
        not_before=timezone.now() + timedelta(seconds=300),
    )

    call_command("sweep_tasks")
    assert calls["n"] == 0


@pytest.mark.django_db(transaction=True)
def test_sweep_does_not_double_dispatch_a_fresh_task():
    """A never-attempted task's announcement belongs to start(). If the
    sweep re-announced those too, every task created between two sweeps
    would run twice — and on a priced surface, bill twice."""
    from django.core.management import call_command

    calls = {"n": 0}
    register_task("fresh.op", lambda p: calls.__setitem__("n", calls["n"] + 1))
    TaskRecord.objects.create(
        kind="fresh.op", state=TaskRecord.PENDING, attempts=0
    )

    call_command("sweep_tasks")
    assert calls["n"] == 0


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


@pytest.mark.django_db(transaction=True)
def test_a_reply_that_does_not_fit_the_transport_is_parked_not_retried(settings):
    """Work that is already done and paid for is never re-done for free.

    The owner's stand, 2026-09-09: ``llm.transcribe`` over a 148-minute
    meeting answered 8 637 982 bytes against an 8 388 608-byte cap. The
    handler raised FunctionPayloadTooLarge AFTER the STT provider had
    transcribed (and billed) the whole recording, and the task runner
    retried it — three attempts inside each of two stage retries, six
    transcriptions of one meeting, 75% of the quota spent on answers nobody
    ever received. The size is not a transient condition: the same input
    produces the same oversized reply every time.
    """
    from stapel_core.comm.exceptions import FunctionPayloadTooLarge

    settings.STAPEL_COMM = {
        **getattr(settings, "STAPEL_COMM", {}), "TASK_RETRY_BACKOFF_BASE": 0,
    }
    calls = {"n": 0}

    def transcribes_then_cannot_answer(payload):
        calls["n"] += 1
        raise FunctionPayloadTooLarge(
            "llm.transcribe", 8_637_982, 8_388_608, direction="reply"
        )

    register_task("llm.transcribe", transcribes_then_cannot_answer)
    with transaction.atomic():
        task_id = start("llm.transcribe", {"audio_url": "https://s3/a.opus"},
                        max_attempts=3)

    st = status(task_id)
    assert calls["n"] == 1, "the provider must be paid exactly once"
    assert st.state == TaskRecord.FAILED
    assert st.attempts == 1, "an oversized reply must not spend the ladder"
    record = TaskRecord.objects.get(pk=task_id)
    assert record.failure_reason == TaskRecord.REASON_UNPROCESSABLE
    # The numbers land on the ROW: the operator who opens the failed task
    # must see which cap was hit without reading another host's log.
    assert "8637982" in record.error and "8388608" in record.error
    assert "max_payload" in record.error and "REFERENCE" in record.error
    assert _emitted[-1].event_type == TASK_FAILED
    assert _emitted[-1].payload["reason"] == TaskRecord.REASON_UNPROCESSABLE


@pytest.mark.django_db(transaction=True)
def test_an_ordinary_transport_failure_still_retries(settings):
    """The narrow rule stays narrow: only the size refusal is unprocessable.

    A FunctionCallError from a provider that was merely down is exactly the
    failure retries exist for.
    """
    from stapel_core.comm.exceptions import FunctionCallError

    settings.STAPEL_COMM = {
        **getattr(settings, "STAPEL_COMM", {}), "TASK_RETRY_BACKOFF_BASE": 0,
    }
    calls = {"n": 0}

    def flaky(payload):
        calls["n"] += 1
        raise FunctionCallError("function 'llm.transcribe' unreachable")

    register_task("llm.flaky", flaky)
    with transaction.atomic():
        task_id = start("llm.flaky", max_attempts=2)
    # The first attempt requeued; run the ladder out.
    TaskRecord.objects.filter(pk=task_id).update(not_before=None)
    execute(task_id)

    assert calls["n"] == 2
    st = status(task_id)
    assert st.state == TaskRecord.FAILED
    assert TaskRecord.objects.get(pk=task_id).failure_reason == (
        TaskRecord.REASON_HANDLER
    )


@pytest.mark.django_db(transaction=True)
def test_the_size_refusal_survives_the_inprocess_call_seam(settings):
    """call() must not launder the class away.

    The task handler above does not raise this by hand in production — it
    calls a Function, and the transport raises. In-process, ``call()`` used
    to wrap EVERY provider exception in a plain FunctionCallError, so the
    task runner saw an anonymous failure and retried the paid work.
    """
    from stapel_core.comm import call, register_function
    from stapel_core.comm.exceptions import FunctionPayloadTooLarge

    settings.STAPEL_COMM = {
        **getattr(settings, "STAPEL_COMM", {}), "TASK_RETRY_BACKOFF_BASE": 0,
    }
    calls = {"n": 0}

    def provider(payload):
        calls["n"] += 1
        raise FunctionPayloadTooLarge(
            "llm.transcribe", 9_000_000, 8_388_608, direction="reply"
        )

    register_function("llm.transcribe", provider)
    register_task("recordings.transcribe", lambda p: call("llm.transcribe", p))

    with transaction.atomic():
        task_id = start("recordings.transcribe", {"audio_url": "x"},
                        max_attempts=3)

    assert calls["n"] == 1
    assert status(task_id).attempts == 1
    assert TaskRecord.objects.get(pk=task_id).failure_reason == (
        TaskRecord.REASON_UNPROCESSABLE
    )
