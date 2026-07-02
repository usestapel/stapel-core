"""Task primitive — asynchronous named operation with persistent state.

The third comm primitive (docs: module-communication.md §2.1): "start work
now, the result arrives later" — long LLM jobs, media processing, exports.
Not a Future over the bus: the waiter is the SYSTEM, not a caller instance,
so state lives in a table and completion is announced with ordinary Actions.

    # caller — returns immediately
    task_id = start("llm.summarize", {"doc_id": 42})

    # owner of the name — registers the executor
    @task_handler("llm.summarize")
    def summarize(payload: dict) -> dict: ...

    # result: poll, subscribe, or callback
    status(task_id)                       # TaskStatus dataclass
    @on_action("task.completed")          # filter by payload["kind"]
    start(..., callback="notify.user")    # Function called with the result

Guarantees: start() persists the record and emits ``task.requested``
through the outbox — the task exists iff the caller's transaction
committed. Execution claims the record atomically (a redelivered
``task.requested`` is a no-op unless the task is PENDING), retries up to
``max_attempts`` and then parks it FAILED with a ``task.failed`` Action.
``manage.py sweep_tasks`` fails tasks past their deadline.

Two orthogonal settings control the pipeline:

Dispatch (STAPEL_COMM["TASK_DISPATCH"]) — how ``task.requested`` REACHES
the worker process: "action" (default) rides ACTION_TRANSPORT like any
other Action; "bus" publishes ``task.*`` events directly via
``stapel_core.bus`` regardless of ACTION_TRANSPORT, so a monolith can keep
Actions in-process while Tasks go through a broker to a dedicated worker
(the outbox row is still written — the transactional guarantee stands);
"inline" makes start() execute the task synchronously via the inline
executor path — for tests and scripts only.

Executors (STAPEL_COMM["TASK_EXECUTOR"]) — how the worker RUNS the handler
once the requested-event arrived: "inline" runs it where the event is
consumed (outbox relay / bus consumer — NOT the web request); "celery"
dispatches to a Celery worker; a dotted path receives ``(task_id)`` for
anything else.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from .config import comm_setting
from .exceptions import CommError

logger = logging.getLogger(__name__)

TASK_REQUESTED = "task.requested"
TASK_COMPLETED = "task.completed"
TASK_FAILED = "task.failed"

_handlers: dict[str, Callable[[dict], Any]] = {}


class TaskNotRegistered(CommError):
    """No local handler owns this task kind."""


class TaskNotFound(CommError):
    """Unknown task_id."""


def task_handler(kind: str) -> Callable:
    """Decorator: register the single executor for task *kind*."""

    def decorator(fn: Callable[[dict], Any]) -> Callable[[dict], Any]:
        register_task(kind, fn)
        return fn

    return decorator


def register_task(kind: str, fn: Callable[[dict], Any]) -> None:
    existing = _handlers.get(kind)
    if existing is not None and existing is not fn:
        raise ValueError(f"task kind '{kind}' already registered by {existing!r}")
    _handlers[kind] = fn


def registered_kinds() -> list[str]:
    return sorted(_handlers)


def clear_handlers() -> None:
    """Tests only."""
    _handlers.clear()


@dataclass
class TaskStatus:
    """Snapshot of a task's state.

    Attributes:
        task_id: UUID string. Example: "6f1f..."
        kind: Task name. Example: llm.summarize
        state: pending | running | done | failed
        result: Handler return value (done only)
        error: repr of the last failure
        attempts: Executions so far
    """

    task_id: str
    kind: str
    state: str
    result: Any = None
    error: str = ""
    attempts: int = 0


def start(
    kind: str,
    payload: dict | None = None,
    *,
    max_attempts: int = 3,
    deadline_seconds: int | None = None,
    correlation_id: str = "",
    callback: str = "",
) -> str:
    """Create the task and announce it. Returns task_id immediately.

    Inside transaction.atomic() the record and its requested-event commit
    (or roll back) with the caller's changes.
    """
    from django.utils import timezone

    from ..django.taskstore.models import TaskRecord
    from .actions import emit

    deadline = None
    if deadline_seconds:
        from datetime import timedelta

        deadline = timezone.now() + timedelta(seconds=deadline_seconds)

    record = TaskRecord.objects.create(
        kind=kind,
        payload=payload or {},
        max_attempts=max_attempts,
        deadline=deadline,
        correlation_id=correlation_id,
        callback=callback,
    )
    emit(
        TASK_REQUESTED,
        {"task_id": str(record.pk), "kind": kind},
        key=correlation_id or str(record.pk),
    )
    if comm_setting("TASK_DISPATCH", "action") == "inline":
        # Tests/scripts: run right here, synchronously, via the inline
        # executor path. The emitted event above stays (outbox audit
        # trail); its redelivery is a no-op — the record is no longer
        # PENDING.
        execute(str(record.pk))
    return str(record.pk)


def status(task_id: str) -> TaskStatus:
    from ..django.taskstore.models import TaskRecord

    record = TaskRecord.objects.filter(pk=task_id).first()
    if record is None:
        raise TaskNotFound(f"no task {task_id!r}")
    return TaskStatus(
        task_id=str(record.pk),
        kind=record.kind,
        state=record.state,
        result=record.result,
        error=record.error,
        attempts=record.attempts,
    )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def handle_task_requested(event) -> None:
    """Framework subscriber for ``task.requested`` (wired by the taskstore
    app). Kinds not registered in this process belong to another service —
    silently skipped."""
    kind = event.payload.get("kind", "")
    task_id = event.payload.get("task_id", "")
    if not task_id or kind not in _handlers:
        return
    _dispatch(task_id)


def _dispatch(task_id: str) -> None:
    executor = comm_setting("TASK_EXECUTOR", "inline")
    if executor == "inline":
        execute(task_id)
        return
    if executor == "celery":
        _celery_execute.delay(task_id)  # type: ignore[union-attr]
        return
    from django.utils.module_loading import import_string

    import_string(executor)(task_id)


try:  # celery executor is optional
    from celery import shared_task

    @shared_task(name="stapel_core.comm.tasks.execute")
    def _celery_execute(task_id: str) -> None:
        execute(task_id)

except ImportError:  # pragma: no cover
    _celery_execute = None


def execute(task_id: str) -> None:
    """Claim and run one task. Safe under at-least-once redelivery: only a
    PENDING record can be claimed."""
    from django.db import transaction
    from django.utils import timezone

    from ..django.taskstore.models import TaskRecord
    from .actions import emit

    with transaction.atomic():
        record = (
            TaskRecord.objects.select_for_update()
            .filter(pk=task_id, state=TaskRecord.PENDING)
            .first()
        )
        if record is None:
            return
        record.state = TaskRecord.RUNNING
        record.attempts += 1
        record.started_at = timezone.now()
        record.save(update_fields=["state", "attempts", "started_at"])

    handler = _handlers.get(record.kind)
    if handler is None:  # requested-event routed here by mistake
        _park(record, "no local handler", emit)
        return

    try:
        result = handler(record.payload)
    except Exception as exc:
        logger.exception("task %s (%s) failed", task_id, record.kind)
        if record.attempts < record.max_attempts:
            TaskRecord.objects.filter(pk=record.pk).update(
                state=TaskRecord.PENDING, error=repr(exc)[:2000]
            )
            # Re-announce through the outbox so the retry survives crashes.
            emit(TASK_REQUESTED, {"task_id": str(record.pk), "kind": record.kind})
        else:
            _park(record, repr(exc)[:2000], emit)
        return

    record.state = TaskRecord.DONE
    record.result = result
    record.error = ""
    record.finished_at = timezone.now()
    record.save(update_fields=["state", "result", "error", "finished_at"])
    emit(
        TASK_COMPLETED,
        {
            "task_id": str(record.pk),
            "kind": record.kind,
            "correlation_id": record.correlation_id,
        },
        key=record.correlation_id or str(record.pk),
    )
    _run_callback(record)


def _park(record, error: str, emit) -> None:
    from django.utils import timezone

    record.state = record.FAILED
    record.error = error
    record.finished_at = timezone.now()
    record.save(update_fields=["state", "error", "finished_at"])
    emit(
        TASK_FAILED,
        {
            "task_id": str(record.pk),
            "kind": record.kind,
            "error": error,
            "correlation_id": record.correlation_id,
        },
        key=record.correlation_id or str(record.pk),
    )
    _run_callback(record)


def _run_callback(record) -> None:
    if not record.callback:
        return
    from .functions import call

    try:
        call(
            record.callback,
            {
                "task_id": str(record.pk),
                "kind": record.kind,
                "state": record.state,
                "result": record.result,
                "error": record.error,
            },
        )
    except Exception:
        logger.exception(
            "task %s callback %s failed", record.pk, record.callback
        )


__all__ = [
    "start",
    "status",
    "task_handler",
    "register_task",
    "registered_kinds",
    "execute",
    "TaskStatus",
    "TaskNotFound",
    "TaskNotRegistered",
    "TASK_REQUESTED",
    "TASK_COMPLETED",
    "TASK_FAILED",
]
