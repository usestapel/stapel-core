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
import time
from dataclasses import dataclass
from typing import Any, Callable

from .backoff import DEFAULT_BASE_SECONDS, DEFAULT_CAP_SECONDS, retry_delay
from .config import comm_setting
from .exceptions import CommError

logger = logging.getLogger(__name__)

TASK_REQUESTED = "task.requested"
TASK_COMPLETED = "task.completed"
TASK_FAILED = "task.failed"

# ─── Metrics ──────────────────────────────────────────────────────────
#
# The Task primitive shipped with none, and the consequence is measurable:
# on a client fleet's stand 215 of 276 `moderation.screen` tasks are parked
# FAILED — a 78% failure rate, running since 2026-08-21 — and no dashboard
# could show it, because nothing counted. The rows were there the whole
# time. Finding them required knowing to run the query.
#
# `bus_dlq_total` is reused for the give-up rather than a fourth
# task-specific counter: a deployment already alerts on it (it is the
# metric the ironmemo outage produced), and "work this system gave up on"
# is one question whether the work was an Action or a Task. The topic label
# is `task.<kind>` so the two producers stay separable in a query.
TASK_STARTED_METRIC = "comm_task_started_total"
TASK_COMPLETED_METRIC = "comm_task_completed_total"
TASK_RETRIED_METRIC = "comm_task_retried_total"
TASK_FAILED_METRIC = "comm_task_failed_total"
TASK_DURATION_METRIC = "comm_task_duration_seconds"


def _metric(fn_name: str, name: str, *args, **kwargs) -> None:
    """Record one metric. Never raises — every call site is on a path whose
    job is something else, and half of them are already failure paths."""
    try:
        from ..observability import metrics

        getattr(metrics, fn_name)(name, *args, **kwargs)
    except Exception:  # pragma: no cover - the facade already guards itself
        logger.debug("comm: task metric %s not recorded", name, exc_info=True)


def _park_in_dlq(kind: str, reason: str) -> None:
    """Count a given-up task in the same series the bus parks Actions in."""
    try:
        from ..bus.dlq import record_parked

        record_parked(f"task.{kind}", reason=reason)
    except Exception:  # pragma: no cover
        logger.debug("comm: task DLQ not recorded", exc_info=True)

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
    dedupe_key: str = "",
) -> str:
    """Create the task and announce it. Returns task_id immediately.

    Inside transaction.atomic() the record and its requested-event commit
    (or roll back) with the caller's changes.

    *dedupe_key* makes the call idempotent: while a task with that key is
    still PENDING or RUNNING, a second ``start()`` returns the FIRST
    task's id and creates nothing. This is the difference between a
    double-clicked publish costing one vision draft and costing two —
    priced surfaces make "the caller retried" a line on an invoice, and
    the caller is often a redelivered message rather than a person.

    The key is deliberately released once the task reaches DONE or
    FAILED: it deduplicates work IN FLIGHT, and does not claim to
    remember forever that some payload was ever run. A caller that needs
    "exactly once, ever" owns a unique constraint on its own table —
    that is a business fact, and this table is a journal.
    """
    from django.db import IntegrityError, transaction
    from django.utils import timezone

    from ..django.taskstore.models import TaskRecord
    from .actions import mutate_and_emit

    deadline = None
    if deadline_seconds:
        from datetime import timedelta

        deadline = timezone.now() + timedelta(seconds=deadline_seconds)

    if dedupe_key:
        live = (
            TaskRecord.objects.filter(
                dedupe_key=dedupe_key,
                state__in=[TaskRecord.PENDING, TaskRecord.RUNNING],
            )
            .values_list("pk", flat=True)
            .first()
        )
        if live is not None:
            logger.info(
                "task %s (%s) already live for dedupe_key=%s — reusing it",
                live, kind, dedupe_key,
            )
            return str(live)

    # Record + requested-event are one transaction (joining the caller's
    # atomic block when present): a task must never exist unannounced, nor
    # be announced without existing.
    try:
        with mutate_and_emit() as emit_event:
            record = TaskRecord.objects.create(
                kind=kind,
                payload=payload or {},
                max_attempts=max_attempts,
                deadline=deadline,
                correlation_id=correlation_id,
                callback=callback,
                dedupe_key=dedupe_key,
            )
            emit_event(
                TASK_REQUESTED,
                {"task_id": str(record.pk), "kind": kind},
                key=correlation_id or str(record.pk),
            )
    except IntegrityError:
        # Two callers raced past the SELECT above. The partial unique index
        # is the arbiter — one insert wins, the loser reads the winner's id
        # rather than surfacing a database error for what is, from the
        # caller's side, the idempotency it asked for.
        if not dedupe_key:
            raise
        with transaction.atomic():
            live = (
                TaskRecord.objects.filter(
                    dedupe_key=dedupe_key,
                    state__in=[TaskRecord.PENDING, TaskRecord.RUNNING],
                )
                .values_list("pk", flat=True)
                .first()
            )
        if live is None:  # pragma: no cover - the winner finished instantly
            raise
        logger.info(
            "task %s (%s) won the dedupe_key=%s race — reusing it",
            live, kind, dedupe_key,
        )
        return str(live)

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
        if _celery_execute is None:
            # Configured for celery, celery not installed. Left as an
            # AttributeError on None this surfaced as
            # `'NoneType' object has no attribute 'delay'` from inside an
            # action handler — a message that names neither the setting nor
            # the missing package, on a path whose failure mode is "no task
            # ever runs again". Say which knob is wrong.
            raise CommError(
                "STAPEL_COMM['TASK_EXECUTOR'] = 'celery' but celery is not "
                "installed in this process. Install it (or the host's "
                "[celery] extra), or set TASK_EXECUTOR to 'inline'."
            )
        _celery_execute.delay(task_id)
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
    PENDING record whose backoff has expired can be claimed."""
    from django.core.exceptions import ValidationError
    from django.db import transaction
    from django.db.models import Q
    from django.utils import timezone

    from ..django.taskstore.models import TaskRecord

    with transaction.atomic():
        record = (
            TaskRecord.objects.select_for_update()
            .filter(pk=task_id, state=TaskRecord.PENDING)
            .filter(Q(not_before__isnull=True) | Q(not_before__lte=timezone.now()))
            .first()
        )
        if record is None:
            # Either somebody else claimed it, or its backoff has not
            # expired. Both are ordinary: leave the row PENDING and let the
            # sweep re-announce it when it comes due. Sleeping here instead
            # would hold a worker hostage to a provider outage.
            return
        record.state = TaskRecord.RUNNING
        record.attempts += 1
        record.started_at = timezone.now()
        record.save(update_fields=["state", "attempts", "started_at"])

    _metric("counter", TASK_STARTED_METRIC, labels={"kind": record.kind})

    handler = _handlers.get(record.kind)
    if handler is None:  # requested-event routed here by mistake
        _park(record, "no local handler", reason=TaskRecord.REASON_NO_HANDLER)
        return

    started = time.monotonic()
    try:
        result = handler(record.payload)
    except ValidationError as exc:
        # The handler decoded the payload, reached working code, and that
        # code refused its VALUES. Retrying reproduces the refusal exactly,
        # three times, and on a priced surface bills for all three. Park it
        # on the first refusal with a reason that says which kind of broken
        # this is — the same rule Actions have followed since 0.53, now
        # applied to the primitive that actually calls the paid provider.
        logger.warning(
            "task %s (%s) refused its payload — parking unprocessable, not retrying",
            task_id, record.kind,
        )
        _observe_duration(record.kind, started)
        _park(record, repr(exc)[:2000], reason=TaskRecord.REASON_UNPROCESSABLE)
        return
    except Exception as exc:
        logger.exception("task %s (%s) failed", task_id, record.kind)
        _observe_duration(record.kind, started)
        if record.attempts < record.max_attempts:
            _requeue(record, repr(exc)[:2000])
        else:
            _park(record, repr(exc)[:2000], reason=TaskRecord.REASON_HANDLER)
        return

    _observe_duration(record.kind, started)
    _metric("counter", TASK_COMPLETED_METRIC, labels={"kind": record.kind})

    from .actions import mutate_and_emit

    # DONE state + completed-event commit together — a task must never be
    # marked DONE with its announcement lost (or vice versa).
    with mutate_and_emit() as emit_event:
        record.state = TaskRecord.DONE
        record.result = result
        record.error = ""
        record.finished_at = timezone.now()
        record.save(update_fields=["state", "result", "error", "finished_at"])
        emit_event(
            TASK_COMPLETED,
            {
                "task_id": str(record.pk),
                "kind": record.kind,
                "correlation_id": record.correlation_id,
            },
            key=record.correlation_id or str(record.pk),
        )
    _run_callback(record)


def _observe_duration(kind: str, started: float) -> None:
    _metric(
        "histogram",
        TASK_DURATION_METRIC,
        time.monotonic() - started,
        labels={"kind": kind},
    )


def retry_delay_for(attempt: int) -> float:
    """Backoff for the next attempt, per this deployment's ladder.

    Exposed so a host can assert what its own configuration produces, and
    so the value is computed in ONE place rather than at each transition.
    """
    return retry_delay(
        attempt,
        base=float(comm_setting("TASK_RETRY_BACKOFF_BASE", DEFAULT_BASE_SECONDS)),
        cap=float(comm_setting("TASK_RETRY_BACKOFF_CAP", DEFAULT_CAP_SECONDS)),
    )


def _requeue(record, error: str) -> None:
    """Handled-failure transition: back to PENDING, held for a jittered
    backoff, and re-announced atomically (the re-announce rides the outbox
    so the retry survives crashes).

    The announcement still goes out immediately. It is the RECORD that is
    held: `execute()` will decline to claim it until `not_before` passes,
    and the sweep re-announces it once it comes due. Announcing late instead
    would need a broker that can schedule delivery, which NATS JetStream
    cannot; holding the row needs only a column, and it holds across a
    worker crash, which a sleeping thread does not.
    """
    from datetime import timedelta

    from django.utils import timezone

    from .actions import mutate_and_emit

    delay = retry_delay_for(record.attempts)
    not_before = timezone.now() + timedelta(seconds=delay)

    _metric("counter", TASK_RETRIED_METRIC, labels={"kind": record.kind})
    logger.info(
        "task %s (%s) attempt %s/%s failed — retrying in %.1fs",
        record.pk, record.kind, record.attempts, record.max_attempts, delay,
    )

    with mutate_and_emit() as emit_event:
        type(record).objects.filter(pk=record.pk).update(
            state=record.PENDING, error=error, not_before=not_before
        )
        emit_event(TASK_REQUESTED, {"task_id": str(record.pk), "kind": record.kind})


def _park(record, error: str, *, reason: str = "") -> None:
    from django.utils import timezone

    from .actions import mutate_and_emit

    with mutate_and_emit() as emit_event:
        record.state = record.FAILED
        record.error = error
        record.failure_reason = reason
        record.finished_at = timezone.now()
        record.save(
            update_fields=["state", "error", "failure_reason", "finished_at"]
        )
        emit_event(
            TASK_FAILED,
            {
                "task_id": str(record.pk),
                "kind": record.kind,
                "error": error,
                "reason": reason,
                "correlation_id": record.correlation_id,
            },
            key=record.correlation_id or str(record.pk),
        )
    _metric(
        "counter",
        TASK_FAILED_METRIC,
        labels={"kind": record.kind, "reason": reason or "unrecorded"},
    )
    _park_in_dlq(record.kind, reason or "handler")
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
    "retry_delay_for",
    "TaskStatus",
    "TaskNotFound",
    "TaskNotRegistered",
    "TASK_REQUESTED",
    "TASK_COMPLETED",
    "TASK_FAILED",
    "TASK_STARTED_METRIC",
    "TASK_COMPLETED_METRIC",
    "TASK_RETRIED_METRIC",
    "TASK_FAILED_METRIC",
    "TASK_DURATION_METRIC",
]
