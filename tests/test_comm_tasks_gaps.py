"""Branch coverage for the comm Task primitive: decorator registration,
executor selection, orphaned tasks, deadlines and callback failures."""
import uuid
from types import SimpleNamespace

import pytest

from stapel_core.comm import task_handler
from stapel_core.comm import tasks as tasks_mod
from stapel_core.comm.registry import action_registry, function_registry
from stapel_core.comm.tasks import (
    TASK_FAILED,
    clear_handlers,
    execute,
    register_task,
    registered_kinds,
    start,
    status,
)
from stapel_core.django.taskstore.models import TaskRecord

_emitted = []


@pytest.fixture(autouse=True)
def clean():
    clear_handlers()
    action_registry.clear()
    function_registry.clear()
    _emitted.clear()
    from stapel_core.comm.actions import subscribe_action

    subscribe_action(TASK_FAILED, _emitted.append)
    yield
    clear_handlers()
    action_registry.clear()
    function_registry.clear()
    _emitted.clear()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_task_handler_decorator_registers_and_returns_fn():
    @task_handler("report.build")
    def build(payload):
        return {"ok": True}

    assert registered_kinds() == ["report.build"]
    assert build({"x": 1}) == {"ok": True}  # decorator returns the fn unwrapped


def test_task_handler_duplicate_kind_rejected():
    register_task("report.build", lambda p: 1)
    with pytest.raises(ValueError, match="already registered"):
        register_task("report.build", lambda p: 2)


def test_reregistering_same_fn_is_noop():
    def fn(payload):
        return 1

    register_task("report.build", fn)
    register_task("report.build", fn)  # same object — allowed
    assert registered_kinds() == ["report.build"]


# ---------------------------------------------------------------------------
# start() — deadline
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_start_with_deadline_seconds_sets_deadline():
    from datetime import timedelta

    from django.utils import timezone

    before = timezone.now()
    task_id = start("llm.summarize", {"doc": 1}, deadline_seconds=60)
    record = TaskRecord.objects.get(pk=task_id)
    assert record.deadline is not None
    assert before + timedelta(seconds=59) <= record.deadline <= timezone.now() + timedelta(seconds=61)


@pytest.mark.django_db(transaction=True)
def test_start_without_deadline_leaves_it_null():
    task_id = start("llm.summarize")
    assert TaskRecord.objects.get(pk=task_id).deadline is None


# ---------------------------------------------------------------------------
# Executor selection (_dispatch)
# ---------------------------------------------------------------------------


def test_dispatch_inline_executes_directly(settings, monkeypatch):
    settings.STAPEL_COMM = {"TASK_EXECUTOR": "inline"}
    ran = []
    monkeypatch.setattr(tasks_mod, "execute", ran.append)
    tasks_mod._dispatch("task-1")
    assert ran == ["task-1"]


def test_dispatch_celery_uses_delay(settings, monkeypatch):
    settings.STAPEL_COMM = {"TASK_EXECUTOR": "celery"}
    delayed = []
    monkeypatch.setattr(
        tasks_mod, "_celery_execute", SimpleNamespace(delay=delayed.append)
    )
    tasks_mod._dispatch("task-2")
    assert delayed == ["task-2"]


def test_dispatch_dotted_path_executor(settings, monkeypatch):
    # import_string resolves against the live module, so the monkeypatched
    # attribute is what the dotted path lands on.
    settings.STAPEL_COMM = {"TASK_EXECUTOR": "stapel_core.comm.tasks.execute"}
    ran = []
    monkeypatch.setattr(tasks_mod, "execute", ran.append)
    tasks_mod._dispatch("task-3")
    assert ran == ["task-3"]


def test_celery_task_body_calls_execute(monkeypatch):
    ran = []
    monkeypatch.setattr(tasks_mod, "execute", ran.append)
    tasks_mod._celery_execute("task-4")  # direct call runs the body eagerly
    assert ran == ["task-4"]


# ---------------------------------------------------------------------------
# execute() — orphaned kind, callback failure
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_execute_without_local_handler_parks_failed():
    record = TaskRecord.objects.create(kind="ghost.kind", payload={})
    execute(str(record.pk))
    record.refresh_from_db()
    assert record.state == TaskRecord.FAILED
    assert record.error == "no local handler"
    assert [e.event_type for e in _emitted] == [TASK_FAILED]
    assert record.attempts == 1


@pytest.mark.django_db(transaction=True)
def test_execute_missing_or_claimed_record_is_noop():
    # unknown id — nothing to claim
    execute(str(uuid.uuid4()))
    # non-PENDING record — redelivery no-op
    record = TaskRecord.objects.create(kind="ghost.kind", state=TaskRecord.DONE)
    execute(str(record.pk))
    record.refresh_from_db()
    assert record.state == TaskRecord.DONE
    assert record.attempts == 0


@pytest.mark.django_db(transaction=True)
def test_callback_failure_does_not_break_completion():
    register_task("math.double", lambda p: {"value": p["n"] * 2})
    # callback function is never registered — call() raises inside _run_callback
    task_id = start("math.double", {"n": 2}, callback="missing.callback")
    execute(task_id)
    st = status(task_id)
    assert st.state == TaskRecord.DONE
    assert st.result == {"value": 4}


@pytest.mark.django_db(transaction=True)
def test_callback_invoked_with_result_on_success():
    from stapel_core.comm.functions import register_function

    seen = []
    register_task("math.double", lambda p: {"value": p["n"] * 2})
    register_function("notify.done", lambda p: seen.append(p) or {})
    task_id = start("math.double", {"n": 3}, callback="notify.done")
    execute(task_id)
    assert len(seen) == 1
    assert seen[0]["task_id"] == task_id
    assert seen[0]["state"] == TaskRecord.DONE
    assert seen[0]["result"] == {"value": 6}
