"""A worker started with -Q that drops its own default queue must not boot.

The live shape (client stand, 2026-09-09): a service's worker ran as
``celery -A core worker -Q cdn,thumbnails,previews,celery`` while its settings
said ``CELERY_TASK_DEFAULT_QUEUE = "svc_default"``. ``-Q`` replaces the
consumed set, so the default queue was consumed by nobody: 27,234 messages
over months, and every retry held on ``not_before`` waited forever because the
task that wakes it — the taskstore sweep — publishes there too.
"""
import logging

import pytest
from celery import Celery, signals

from stapel_core.django import celery_consumer_guard as guard

DEFAULT_QUEUE = "svc_default"


@pytest.fixture
def worker():
    """A Celery app plus the worker-shaped object ``celeryd_after_setup``
    carries, with the guard connected exactly as a worker gets it.

    No broker: ``app.amqp.queues.select()`` is what ``-Q`` calls, and it only
    rewrites an in-memory mapping.
    """
    guard._installed = False
    guard.install(force=True)

    class _Worker:
        def __init__(self, app):
            self.app = app

    def _start(selected=None, *, routes=None, default=DEFAULT_QUEUE):
        app = Celery("svc")
        app.conf.task_default_queue = default
        if routes is not None:
            app.conf.task_routes = routes
        if selected is not None:
            app.select_queues(selected)  # what `-Q` does, verbatim
        signals.celeryd_after_setup.send(
            sender="worker@stand", instance=_Worker(app), conf=app.conf
        )

    yield _start

    signals.celeryd_after_setup.disconnect(dispatch_uid=guard._UID)
    guard._installed = False


def test_a_bare_worker_consumes_the_default_and_is_silent(worker, caplog):
    """No -Q: consume_from is every declared queue, the default included."""
    with caplog.at_level(logging.WARNING):
        worker(None)
    assert caplog.records == []


def test_minus_q_that_includes_the_default_is_silent(worker, caplog):
    with caplog.at_level(logging.WARNING):
        worker([DEFAULT_QUEUE, "cdn", "thumbnails"])
    assert caplog.records == []


def test_minus_q_without_the_default_refuses_to_start(worker):
    """The defect itself — and it must travel through the real signal.

    Celery's dispatcher swallows every ``Exception`` a receiver raises, so
    this asserts the whole chain: the guard is connected by ``install()``,
    and what it raises is a ``BaseException`` that reaches the caller of
    ``send()`` (i.e. ``Worker.on_start``) instead of being logged and
    ignored.
    """
    with pytest.raises(guard.PartialConsumerError) as excinfo:
        worker(["cdn", "thumbnails", "previews", "celery"])

    message = excinfo.value.message
    assert f'default queue "{DEFAULT_QUEUE}"' in message
    # the consumed set, named
    for consumed in ("cdn", "thumbnails", "previews", "celery"):
        assert f'"{consumed}"' in message
    # both repairs
    assert "drop -Q" in message
    assert f"-Q {DEFAULT_QUEUE},cdn,celery,previews,thumbnails" in message
    assert guard.ALLOW_PARTIAL_SETTING in message
    # SystemExit: the exit code carries the message to stderr on its own.
    assert isinstance(excinfo.value, SystemExit)
    assert excinfo.value.code == message


def test_the_escape_setting_warns_instead_of_raising(worker, caplog, settings):
    setattr(settings, guard.ALLOW_PARTIAL_SETTING, True)
    with caplog.at_level(logging.WARNING):
        worker(["cdn", "thumbnails"])  # no raise
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1
    assert f'default queue "{DEFAULT_QUEUE}"' in warnings[0]
    assert "Some worker in this deployment must consume" in warnings[0]
    assert "only the process it runs in" in warnings[0]


def test_a_routed_queue_this_worker_misses_is_a_warning_not_a_refusal(
    worker, caplog
):
    with caplog.at_level(logging.WARNING):
        worker(
            [DEFAULT_QUEUE, "cdn"],
            routes={"pkg.tasks.render": {"queue": "thumbnails"}},
        )
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1
    assert '"thumbnails"' in warnings[0]
    assert '"pkg.tasks.render"' in warnings[0]
    assert "Warning, not a refusal" in warnings[0]


def test_a_routed_queue_this_worker_consumes_is_silent(worker, caplog):
    with caplog.at_level(logging.WARNING):
        worker(
            [DEFAULT_QUEUE, "thumbnails"],
            routes={"pkg.tasks.render": {"queue": "thumbnails"}},
        )
    assert caplog.records == []


def test_the_default_queue_refusal_wins_over_the_routed_warning(worker, caplog):
    """A worker missing both says the loud thing; the warning never runs."""
    with pytest.raises(guard.PartialConsumerError):
        worker(["cdn"], routes={"pkg.tasks.render": {"queue": "thumbnails"}})


# ─── the pieces, without the signal ──────────────────────────────────────


def test_routes_are_read_from_every_declarative_shape():
    assert guard.routed_queues({"a.b": {"queue": "q1"}}) == {"q1": ["a.b"]}
    assert guard.routed_queues([{"a.b": {"queue": "q1"}}]) == {"q1": ["a.b"]}
    assert guard.routed_queues([("a.b", {"queue": "q1"})]) == {"q1": ["a.b"]}
    # A router callable or dotted path is host code; the guard does not call it.
    assert guard.routed_queues("pkg.routers.route") == {}
    assert guard.routed_queues(None) == {}
    # An entry that names no queue (rate limits, exchanges) is not a queue.
    assert guard.routed_queues({"a.b": {"rate_limit": "1/s"}}) == {}


def test_no_default_queue_configured_is_not_a_refusal():
    assert guard.verify_consumer_queues(
        default_queue=None, consumed=["cdn"]
    ) == []


def test_an_unreadable_worker_does_not_stop_the_boot(caplog):
    """Reading the worker's own configuration must never be what kills it."""

    class _Broken:
        @property
        def app(self):
            raise RuntimeError("no amqp here")

    with caplog.at_level(logging.ERROR):
        guard._on_worker_setup(sender="worker@stand", instance=_Broken())
    assert any(
        "the default-queue guard did not run" in r.getMessage()
        for r in caplog.records
    )


def test_install_is_idempotent_and_needs_no_host_line():
    guard._installed = False
    try:
        assert guard.install(force=True) is True
        assert guard.install(force=True) is False
        assert guard.is_installed() is True
    finally:
        signals.celeryd_after_setup.disconnect(dispatch_uid=guard._UID)
        guard._installed = False


def test_the_app_ready_installs_the_guard(monkeypatch):
    """No service opts in: stapel_core.django's AppConfig.ready() connects it.

    Without this, every test above would still pass while the guard was
    connected in nothing but the test suite.
    """
    import importlib

    from stapel_core.django.apps import CommonDjangoConfig

    calls = []
    monkeypatch.setattr(guard, "install", lambda *a, **kw: calls.append(True))
    config = CommonDjangoConfig(
        "stapel_core.django", importlib.import_module("stapel_core.django")
    )
    config.ready()
    assert calls, "CommonDjangoConfig.ready() did not install the guard"
