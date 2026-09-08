"""A service's default queue must name the service, and boot smoke says so.

The live shape this reproduces (client stand, 2026-09-08): svc-video's
``config/celery.py`` says ``Celery("video")`` while its settings said
``CELERY_TASK_DEFAULT_QUEUE = "chat"``. Its beat published onto svc-chat's
queue; svc-chat's worker took ~63% of those sweeps and refused every one
(226 tracebacks an hour), and the tasks the two services BOTH register ran in
the wrong process against the wrong database with nothing logged at all.
"""
import pytest
from celery import Celery

from stapel_core.django.celery_checks import (
    E001_QUEUE_NAMES_ANOTHER_APP,
    W002_QUEUE_IS_THE_SHARED_DEFAULT,
    check_task_default_queue,
)


def _ids(findings):
    return [f.id for f in findings]


@pytest.fixture
def app_with(monkeypatch):
    """Bind a project-shaped Celery app the check can read, then unbind it."""

    def _bind(app_name, queue):
        app = Celery(app_name)
        if queue is not None:
            app.conf.task_default_queue = queue
        monkeypatch.setattr("celery.current_app", app, raising=False)
        return app

    return _bind


def test_a_service_that_names_its_own_queue_is_silent(app_with):
    app_with("video", "video")
    assert check_task_default_queue() == []


def test_hyphen_and_underscore_are_one_name(app_with):
    """stapel-tools renders the app from the module and the queue from the
    slug, so every generated multiword service differs by that character."""
    app_with("classified_core", "classified-core")
    assert check_task_default_queue() == []


def test_another_services_queue_is_an_error(app_with):
    """The live defect: the Celery app is video, the queue is chat."""
    app_with("video", "chat")
    findings = check_task_default_queue()
    assert _ids(findings) == [E001_QUEUE_NAMES_ANOTHER_APP]
    assert findings[0].level >= 40  # Error: the damage it names is silent
    assert '"video"' in findings[0].msg and '"chat"' in findings[0].msg
    # The hint states the repair as the line to write.
    assert 'CELERY_TASK_DEFAULT_QUEUE = "video"' in findings[0].hint


def test_the_error_is_silenceable_by_a_deployment_that_means_it(app_with):
    app_with("video", "chat")
    findings = check_task_default_queue()
    assert findings[0].is_silenced() is False
    from django.test import override_settings

    with override_settings(SILENCED_SYSTEM_CHECKS=[E001_QUEUE_NAMES_ANOTHER_APP]):
        assert check_task_default_queue()[0].is_silenced() is True


def test_celerys_own_default_queue_is_a_warning(app_with):
    """A service that never set one shares 'celery' with every other service
    on the broker — but a single-service deployment is entitled to it."""
    app_with("video", None)  # Celery's factory default: "celery"
    findings = check_task_default_queue()
    assert _ids(findings) == [W002_QUEUE_IS_THE_SHARED_DEFAULT]
    assert findings[0].level < 40


def test_no_project_app_bound_reports_nothing(app_with):
    """celery installed but no config/celery.py imported: reading a queue off
    Celery's own module-level app would report the library's default as this
    service's choice."""
    app_with("default", "celery")
    assert check_task_default_queue() == []


def test_celery_absent_reports_nothing(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _no_celery(name, *args, **kwargs):
        if name == "celery":
            raise ImportError("no celery here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_celery)
    assert check_task_default_queue() == []
