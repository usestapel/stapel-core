"""The Task primitive's metrics — the part whose absence hid a 78% failure rate.

Before 0.60 the Task table was the only record that anything had gone
wrong, and reading it required knowing to run the query. On a client
fleet's stand that meant 215 parked `moderation.screen` tasks accumulating
since 2026-08-21 behind an all-green dashboard: every container Up, every
healthcheck passing, three quarters of the screening silently not
happening.

So these tests assert on metric NAMES and LABELS, not just on "a metric was
recorded" — a dashboard is built against the names, and a rename that no
test notices is a dashboard that goes blank without failing anything.
"""
import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction

from stapel_core.comm import start, status
from stapel_core.comm.registry import action_registry
from stapel_core.comm.tasks import (
    TASK_COMPLETED_METRIC,
    TASK_DURATION_METRIC,
    TASK_FAILED_METRIC,
    TASK_REQUESTED,
    TASK_RETRIED_METRIC,
    TASK_STARTED_METRIC,
    clear_handlers,
    handle_task_requested,
    register_task,
)
from stapel_core.django.taskstore.models import TaskRecord
from stapel_core.observability import metrics as metrics_mod
from stapel_core.observability.backends import NoopMetricsBackend


class RecordingBackend(NoopMetricsBackend):
    available = True

    def __init__(self):
        self.calls = []

    def counter(self, name, value=1.0, labels=None, *, description=""):
        self.calls.append(("counter", name, value, dict(labels or {})))

    def gauge(self, name, value, labels=None, *, description=""):
        self.calls.append(("gauge", name, value, dict(labels or {})))

    def histogram(self, name, value, labels=None, *, description="", buckets=None):
        self.calls.append(("histogram", name, value, dict(labels or {})))


@pytest.fixture
def recorded(settings):
    from stapel_core.comm.actions import subscribe_action

    clear_handlers()
    action_registry.clear()
    subscribe_action(TASK_REQUESTED, handle_task_requested)
    settings.STAPEL_COMM = {
        **getattr(settings, "STAPEL_COMM", {}), "TASK_RETRY_BACKOFF_BASE": 0,
    }
    backend = RecordingBackend()
    metrics_mod.set_backend(backend)
    yield backend
    metrics_mod.reset_backend()
    clear_handlers()
    action_registry.clear()


def _names(backend, kind_of_call="counter"):
    return [c[1] for c in backend.calls if c[0] == kind_of_call]


def _labels_for(backend, name):
    return [c[3] for c in backend.calls if c[1].endswith(name)]


@pytest.mark.django_db(transaction=True)
def test_success_counts_started_and_completed_and_times_it(recorded):
    register_task("metric.ok", lambda p: {"ok": True})
    with transaction.atomic():
        start("metric.ok")

    counters = _names(recorded)
    assert any(n.endswith(TASK_STARTED_METRIC) for n in counters)
    assert any(n.endswith(TASK_COMPLETED_METRIC) for n in counters)
    assert any(
        n.endswith(TASK_DURATION_METRIC) for n in _names(recorded, "histogram")
    )
    assert _labels_for(recorded, TASK_STARTED_METRIC) == [{"kind": "metric.ok"}]


@pytest.mark.django_db(transaction=True)
def test_retry_is_counted_separately_from_failure(recorded):
    """A retried task and a given-up task are different operational events:
    one is noise, the other is work being dropped. One counter for both
    cannot tell an operator which is happening."""
    calls = {"n": 0}

    def flaky(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return {"ok": True}

    register_task("metric.flaky", flaky)
    with transaction.atomic():
        start("metric.flaky", max_attempts=3)

    counters = _names(recorded)
    assert any(n.endswith(TASK_RETRIED_METRIC) for n in counters)
    assert not any(n.endswith(TASK_FAILED_METRIC) for n in counters)
    assert any(n.endswith(TASK_COMPLETED_METRIC) for n in counters)


@pytest.mark.django_db(transaction=True)
def test_giving_up_counts_the_reason_and_reaches_the_dlq_series(recorded):
    """The give-up lands in `bus_dlq_total` as well as its own counter.

    A deployment already alerts on that series — it is the metric the
    ironmemo dropped-login-codes outage produced — and "work this system
    gave up on" is one question whether the work was an Action or a Task.
    """
    register_task("metric.broken", lambda p: (_ for _ in ()).throw(RuntimeError("x")))
    with transaction.atomic():
        start("metric.broken", max_attempts=2)

    failed = _labels_for(recorded, TASK_FAILED_METRIC)
    assert {"kind": "metric.broken", "reason": TaskRecord.REASON_HANDLER} in failed

    dlq = _labels_for(recorded, "bus_dlq_total")
    assert {"topic": "task.metric.broken", "reason": TaskRecord.REASON_HANDLER} in dlq


@pytest.mark.django_db(transaction=True)
def test_unprocessable_is_labelled_as_such_not_as_a_handler_error(recorded):
    """The two need different people to do different things — a provider
    outage is an ops page, a payload nothing will accept is a bug report."""
    register_task(
        "metric.poison",
        lambda p: (_ for _ in ()).throw(DjangoValidationError("bad id")),
    )
    with transaction.atomic():
        task_id = start("metric.poison", max_attempts=5)

    assert status(task_id).attempts == 1
    failed = _labels_for(recorded, TASK_FAILED_METRIC)
    assert {
        "kind": "metric.poison",
        "reason": TaskRecord.REASON_UNPROCESSABLE,
    } in failed


@pytest.mark.django_db(transaction=True)
def test_a_broken_metrics_backend_never_breaks_the_task(settings):
    """Metrics are a report on the work, never a precondition for it."""
    from stapel_core.comm.actions import subscribe_action

    clear_handlers()
    action_registry.clear()
    subscribe_action(TASK_REQUESTED, handle_task_requested)

    class Exploding(NoopMetricsBackend):
        available = True

        def counter(self, *a, **k):
            raise RuntimeError("metrics down")

        def histogram(self, *a, **k):
            raise RuntimeError("metrics down")

    metrics_mod.set_backend(Exploding())
    try:
        register_task("metric.resilient", lambda p: {"ok": True})
        with transaction.atomic():
            task_id = start("metric.resilient")
        assert status(task_id).state == TaskRecord.DONE
    finally:
        metrics_mod.reset_backend()
        clear_handlers()
        action_registry.clear()
