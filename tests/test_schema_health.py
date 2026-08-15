"""The schema-drift probe, and the earned properties that are not incidental.

The probe answers "is the running code's schema at head". It was written in a
product after a stand ran twelve hours on an unmigrated database while
reporting healthy; it is framework work, so it lives in core and every service
gets it. These tests pin the properties that cost the incident to learn:

* a determined verdict is cached briefly; a NON-answer is never cached;
* a database error is logged as a warning WITHOUT a stack, anything else WITH
  one (a traceback for a database restart is the noise that teaches people to
  skip the log);
* the ``schema_at_head`` series is OMITTED when undetermined, not dropped to
  zero, so a drift alert has nothing to fire on when nobody could ask;
* it registers NON-critical: drift must not pull every backend out of
  rotation during a normal rolling migration.
"""
import logging

import pytest
from django.db import Error as DatabaseError
from django.test import override_settings

from stapel_core.django.monitoring import health as health_mod
from stapel_core.django.monitoring import schema_health as sh


@pytest.fixture(autouse=True)
def _fresh_state():
    sh.reset_schema_state()
    yield
    sh.reset_schema_state()


# ---------------------------------------------------------------------------
# the three states
# ---------------------------------------------------------------------------


def test_no_pending_migrations_is_at_head(monkeypatch):
    monkeypatch.setattr(sh, "unapplied_migrations", lambda: [])
    assert sh.schema_state() is sh.AT_HEAD
    assert sh.schema_probe() is True


def test_pending_migrations_is_behind(monkeypatch, caplog):
    monkeypatch.setattr(sh, "unapplied_migrations", lambda: ["users.0007_x"])
    with caplog.at_level(logging.ERROR):
        assert sh.schema_state() is sh.BEHIND
    assert "users.0007_x" in caplog.text
    assert sh.schema_probe() is False


def test_database_error_is_unknown_not_behind(monkeypatch):
    """The bug the three states exist for: a connectivity fault used to come
    out as a verdict of 'behind'."""
    def boom():
        raise DatabaseError('could not translate host name "db" to address')

    monkeypatch.setattr(sh, "unapplied_migrations", boom)
    assert sh.schema_state() is sh.UNKNOWN
    assert sh.schema_probe() is None


def test_unexpected_error_is_unknown_too(monkeypatch):
    def boom():
        raise ValueError("inconsistent migration history")

    monkeypatch.setattr(sh, "unapplied_migrations", boom)
    assert sh.schema_state() is sh.UNKNOWN


# ---------------------------------------------------------------------------
# caching: verdicts yes, non-answers never
# ---------------------------------------------------------------------------


def test_a_determined_verdict_is_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(sh, "unapplied_migrations", lambda: calls.append(1) or [])
    sh.schema_state()
    sh.schema_state()
    sh.schema_state()
    assert len(calls) == 1


def test_a_non_answer_is_never_cached(monkeypatch):
    """Pinning 'I could not tell' for the TTL makes a two-second blip outlive
    itself, and the retry costs one failed connection attempt — which the
    endpoint's own database check is already making on the same request."""
    calls = []

    def boom():
        calls.append(1)
        raise DatabaseError("db restarting")

    monkeypatch.setattr(sh, "unapplied_migrations", boom)
    sh.schema_state()
    sh.schema_state()
    assert len(calls) == 2


def test_recovery_is_seen_on_the_next_call(monkeypatch):
    answers = [DatabaseError("db restarting"), []]

    def flaky():
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(sh, "unapplied_migrations", flaky)
    assert sh.schema_state() is sh.UNKNOWN
    assert sh.schema_state() is sh.AT_HEAD


# ---------------------------------------------------------------------------
# log shape
# ---------------------------------------------------------------------------


def test_database_error_logs_a_warning_without_a_stack(monkeypatch, caplog):
    def boom():
        raise DatabaseError("db restarting")

    monkeypatch.setattr(sh, "unapplied_migrations", boom)
    with caplog.at_level(logging.WARNING):
        sh.schema_state()
    record = caplog.records[-1]
    assert record.levelno == logging.WARNING
    assert record.exc_info is None


def test_unexpected_error_logs_with_a_stack(monkeypatch, caplog):
    def boom():
        raise ValueError("broken migration graph")

    monkeypatch.setattr(sh, "unapplied_migrations", boom)
    with caplog.at_level(logging.ERROR):
        sh.schema_state()
    record = caplog.records[-1]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None


# ---------------------------------------------------------------------------
# metrics: the at-head series is absent, not zero
# ---------------------------------------------------------------------------


def test_at_head_series_is_omitted_when_undetermined(monkeypatch):
    def boom():
        raise DatabaseError("db restarting")

    monkeypatch.setattr(sh, "unapplied_migrations", boom)
    with override_settings(SERVICE_NAME="Iron Auth"):
        text = sh._metrics()
    assert 'stapel_schema_probe_ok{service="iron_auth"} 0' in text
    assert "schema_at_head" not in text


def test_at_head_series_carries_the_verdict_when_determined(monkeypatch):
    monkeypatch.setattr(sh, "unapplied_migrations", lambda: [])
    with override_settings(SERVICE_NAME="svc"):
        text = sh._metrics()
    assert 'stapel_schema_probe_ok{service="svc"} 1' in text
    assert 'stapel_schema_at_head{service="svc"} 1' in text

    sh.reset_schema_state()
    monkeypatch.setattr(sh, "unapplied_migrations", lambda: ["users.0007_x"])
    with override_settings(SERVICE_NAME="svc"):
        text = sh._metrics()
    assert 'stapel_schema_at_head{service="svc"} 0' in text


def test_metrics_prefix_is_honoured(monkeypatch):
    monkeypatch.setattr(sh, "unapplied_migrations", lambda: [])
    with override_settings(STAPEL_METRICS_PREFIX="iron_", SERVICE_NAME="svc"):
        text = sh._metrics()
    assert 'iron_schema_at_head{service="svc"} 1' in text


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def test_registers_non_critical_and_is_idempotent(monkeypatch):
    monkeypatch.setattr(health_mod, "_dependency_checks", [])
    monkeypatch.setattr(health_mod, "_custom_metrics_exporters", [])
    monkeypatch.setattr(sh, "_registered", False)

    sh.register_schema_check()
    sh.register_schema_check()

    assert len(health_mod._dependency_checks) == 1
    name, probe, critical = health_mod._dependency_checks[0]
    assert name == "schema"
    assert critical is False, "drift must not pull every backend during a rolling migration"
    assert len(health_mod._custom_metrics_exporters) == 1


def test_every_service_gets_the_probe_without_wiring_it(tmp_path):
    """Registered from ``CommonDjangoConfig.ready()``, so a product that
    installs ``stapel_core.django`` has the answer without remembering to ask.

    Asserting the registry in THIS process would prove nothing — importing the
    module registers nothing, and the test harness does not install the app
    config. The proof is a process that never names ``schema_health``: a
    service that installs the app and boots, which is the whole promise.
    """
    import os
    import subprocess
    import sys
    import textwrap

    (tmp_path / "projsettings.py").write_text(
        textwrap.dedent("""
            SECRET_KEY = "test-only-not-a-real-key-0123456789"
            DATABASES = {"default": {
                "ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
            INSTALLED_APPS = [
                "django.contrib.contenttypes",
                "django.contrib.auth",
                "rest_framework",
                "stapel_core.django.apps.CommonDjangoConfig",
                "stapel_core.django.users",
            ]
            AUTH_USER_MODEL = "users.User"
            ROOT_URLCONF = "projurls"
        """),
        encoding="utf-8",
    )
    (tmp_path / "projurls.py").write_text("urlpatterns = []\n", encoding="utf-8")
    child_env = dict(os.environ)
    child_env["DJANGO_SETTINGS_MODULE"] = "projsettings"
    # tmp_path only: the repo root holds a ``django/`` package directory that
    # would shadow Django itself in the child.
    child_env["PYTHONPATH"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent("""
            import django
            django.setup()
            from stapel_core.django.monitoring.health import _dependency_checks
            names = {name: critical for name, _p, critical in _dependency_checks}
            assert "schema" in names, sorted(names)
            assert names["schema"] is False, "schema drift must not be critical"
            print("OK")
        """)],
        capture_output=True, text=True, cwd=str(tmp_path), env=child_env,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_health_body_says_unknown_when_the_probe_cannot_ask(monkeypatch):
    """End to end over the lifted probe and the third state together."""
    import json

    from django.test import RequestFactory

    class _OkCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql):
            pass

    class _OkConnection:
        def cursor(self):
            return _OkCursor()

    def boom():
        raise DatabaseError("db restarting")

    monkeypatch.setattr(sh, "unapplied_migrations", boom)
    monkeypatch.setattr(health_mod, "connection", _OkConnection())
    monkeypatch.setattr(health_mod, "_dependency_checks", [])
    monkeypatch.setattr(sh, "_registered", False)
    sh.register_schema_check()

    resp = health_mod.health_check(RequestFactory().get("/api/health/"))
    assert resp.status_code == 200
    assert json.loads(resp.content)["checks"]["schema"] == "unknown"
