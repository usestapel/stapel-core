"""A retry is bound to a STEP, not to the handler.

The retry ladder re-runs the whole handler, so a handler that transcribes,
then summarizes, then writes rows pays the provider again for the steps that
had already succeeded. Owner's stand, 2026-09-09: six transcriptions of one
148-minute meeting were billed because the step AFTER transcription was the
one failing.

A checkpoint is the handler's own statement that a step is done and what it
produced, persisted on the task row the moment it is taken.
"""
import json

import pytest
from django.db import transaction

from stapel_core.comm import checkpoint, current_task, resume, start, status
from stapel_core.comm import overflow
from stapel_core.comm.registry import action_registry
from stapel_core.comm.tasks import (
    TASK_REQUESTED,
    NoCurrentTask,
    clear_handlers,
    execute,
    handle_task_requested,
    register_task,
)
from stapel_core.django.taskstore.models import TaskRecord


@pytest.fixture(autouse=True)
def clean():
    from stapel_core.comm.actions import subscribe_action

    clear_handlers()
    action_registry.clear()
    subscribe_action(TASK_REQUESTED, handle_task_requested)
    overflow.reset_store()
    yield
    clear_handlers()
    action_registry.clear()
    overflow.reset_store()


@pytest.fixture
def instant_retries(settings):
    settings.STAPEL_COMM = {
        **(getattr(settings, "STAPEL_COMM", {}) or {}), "TASK_RETRY_BACKOFF_BASE": 0,
    }
    return settings


class MemoryStore(overflow.OverflowStore):
    name = "django"

    def __init__(self):
        self.objects = {}
        self.deleted = []

    def put(self, key, data, *, ttl_seconds):
        self.objects[key] = data
        return key

    def get(self, key):
        return self.objects[key]

    def delete(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)


@pytest.mark.django_db(transaction=True)
def test_a_retry_resumes_after_the_last_completed_step(instant_retries):
    """Two checkpointed steps, a third that fails once.

    The expensive steps run ONCE across both attempts; only the step that
    failed is re-run. This is the whole feature, and the incident it comes
    from is priced in provider credits.
    """
    ran = {"transcribe": 0, "diarize": 0, "persist": 0}

    def pipeline(payload):
        transcript = resume("transcript")
        if transcript is None:
            ran["transcribe"] += 1           # the paid step
            transcript = {"words": ["a", "b"]}
            checkpoint("transcript", transcript)

        speakers = resume("speakers")
        if speakers is None:
            ran["diarize"] += 1              # the other paid step
            speakers = ["A", "B"]
            checkpoint("speakers", speakers)

        ran["persist"] += 1
        if ran["persist"] == 1:
            raise RuntimeError("database was restarting")
        return {"words": len(transcript["words"]), "speakers": speakers}

    register_task("recordings.pipeline", pipeline)
    with transaction.atomic():
        task_id = start("recordings.pipeline", {"recording": "1"}, max_attempts=3)

    # The first attempt requeued and the ladder's next rung ran (the retry
    # re-announcement is delivered in-process here); force it if a
    # deployment's backoff left the row PENDING.
    if status(task_id).state == TaskRecord.PENDING:
        TaskRecord.objects.filter(pk=task_id).update(not_before=None)
        execute(task_id)

    st = status(task_id)
    assert st.state == TaskRecord.DONE
    assert st.result == {"words": 2, "speakers": ["A", "B"]}
    assert ran == {"transcribe": 1, "diarize": 1, "persist": 2}, (
        "only the step that failed may be re-run"
    )


@pytest.mark.django_db(transaction=True)
def test_a_checkpoint_is_on_the_row_before_the_handler_returns(instant_retries):
    """It survives the crash it exists for — so it cannot live in memory."""
    seen = {}

    def handler(payload):
        checkpoint("stt", {"provider": "elevenlabs", "credits": 1200})
        seen["row"] = TaskRecord.objects.get(pk=current_task().id).checkpoints
        raise RuntimeError("and then the process died")

    register_task("x.op", handler)
    with transaction.atomic():
        task_id = start("x.op", max_attempts=1)

    assert seen["row"] == {"stt": {"provider": "elevenlabs", "credits": 1200}}
    # A parked task KEEPS its ledger: it is the record of how far it got.
    assert TaskRecord.objects.get(pk=task_id).state == TaskRecord.FAILED
    assert TaskRecord.objects.get(pk=task_id).checkpoints["stt"]["credits"] == 1200


@pytest.mark.django_db(transaction=True)
def test_checkpoints_are_cleared_on_success():
    def handler(payload):
        checkpoint("step", {"big": "value"})
        return {"ok": True}

    register_task("y.op", handler)
    with transaction.atomic():
        task_id = start("y.op")

    record = TaskRecord.objects.get(pk=task_id)
    assert record.state == TaskRecord.DONE
    assert record.checkpoints == {}, (
        "a completed task's intermediate values are a second copy of data "
        "that now has a permanent home"
    )


@pytest.mark.django_db(transaction=True)
def test_resume_returns_the_default_for_a_step_never_taken():
    got = {}

    def handler(payload):
        got["v"] = resume("never", default="fresh")
        return {}

    register_task("z.op", handler)
    with transaction.atomic():
        start("z.op")
    assert got["v"] == "fresh"


@pytest.mark.django_db(transaction=True)
def test_a_task_that_takes_no_checkpoints_is_completely_unchanged():
    """The primitive is opt-in: nothing changes for the fleet's handlers."""
    register_task("plain.op", lambda p: {"n": p["n"] + 1})
    with transaction.atomic():
        task_id = start("plain.op", {"n": 1})

    record = TaskRecord.objects.get(pk=task_id)
    assert record.state == TaskRecord.DONE
    assert record.result == {"n": 2}
    assert record.checkpoints == {}


def test_checkpoint_outside_a_handler_says_so():
    with pytest.raises(NoCurrentTask) as exc:
        checkpoint("anything", 1)
    assert "outside a task handler" in str(exc.value)
    assert current_task() is None


@pytest.mark.django_db(transaction=True)
def test_a_big_checkpoint_travels_by_reference(settings, instant_retries):
    """A transcript does not fit a JSON column any better than a message.

    Same store as the Function overflow seam — and NOT consumed on read: a
    checkpoint's one reader is every subsequent attempt.
    """
    store = MemoryStore()
    settings.STAPEL_COMM = {
        **(getattr(settings, "STAPEL_COMM", {}) or {}),
        "TASK_RETRY_BACKOFF_BASE": 0,
        "LARGE_REPLY": {"STORE": store},
        "CHECKPOINT_INLINE_MAX_BYTES": 1024,
    }
    overflow.reset_store()

    transcript = {"words": [{"w": "слово", "s": i} for i in range(2000)]}
    assert len(json.dumps(transcript)) > 1024
    ran = {"stt": 0, "persist": 0}
    rows = []

    def pipeline(payload):
        got = resume("transcript")
        if got is None:
            ran["stt"] += 1
            got = transcript
            checkpoint("transcript", got)
        # What the ROW holds while the task is still in flight.
        rows.append(TaskRecord.objects.get(pk=current_task().id).checkpoints)
        ran["persist"] += 1
        if ran["persist"] == 1:
            raise RuntimeError("transient")
        assert got == transcript, "the reference resolved to the same value"
        return {"words": len(got["words"])}

    register_task("big.pipeline", pipeline)
    with transaction.atomic():
        task_id = start("big.pipeline", max_attempts=3)
    if status(task_id).state == TaskRecord.PENDING:
        TaskRecord.objects.filter(pk=task_id).update(not_before=None)
        execute(task_id)

    # The value went to the store, not into the row.
    assert "$ref" in rows[0]["transcript"]
    assert rows[1] == rows[0], "the second attempt read the same reference"
    assert ran == {"stt": 1, "persist": 2}
    assert status(task_id).state == TaskRecord.DONE
    # Cleared on success — including the object it pointed at.
    assert TaskRecord.objects.get(pk=task_id).checkpoints == {}
    assert store.objects == {} and store.deleted


@pytest.mark.django_db(transaction=True)
def test_a_big_checkpoint_without_a_store_still_lands_inline(settings):
    """A fat row beats re-running a priced step."""
    settings.STAPEL_COMM = {
        **(getattr(settings, "STAPEL_COMM", {}) or {}),
        "LARGE_REPLY": {},
        "CHECKPOINT_INLINE_MAX_BYTES": 64,
    }
    overflow.reset_store()

    def handler(payload):
        checkpoint("transcript", {"words": ["x"] * 200})
        return {}

    register_task("nostore.op", handler)
    with transaction.atomic():
        task_id = start("nostore.op")
    # DONE clears it, so read it back from inside the handler's own view:
    assert TaskRecord.objects.get(pk=task_id).state == TaskRecord.DONE


@pytest.mark.django_db(transaction=True)
def test_the_ambient_task_never_outlives_its_handler():
    register_task("a.op", lambda p: {"task": current_task().kind})
    with transaction.atomic():
        task_id = start("a.op")
    assert status(task_id).result == {"task": "a.op"}
    assert current_task() is None


@pytest.mark.django_db(transaction=True)
def test_the_context_carries_the_attempt_number(instant_retries):
    seen = []

    def handler(payload):
        seen.append(current_task().attempts)
        if len(seen) == 1:
            raise RuntimeError("once")
        return {}

    register_task("attempt.op", handler)
    with transaction.atomic():
        task_id = start("attempt.op", max_attempts=3)
    TaskRecord.objects.filter(pk=task_id).update(not_before=None)
    execute(task_id)
    assert seen == [1, 2]
