"""A Celery worker's metrics have to be reachable from outside the worker.

The defect these cover, once: `serve_metrics()` was called from exactly two
places in the framework, and neither of them is a Celery process — so a
deployment that set `EXPORTER_PORT` on a worker container, added the scrape
job and watched the ladder counters stay at zero was looking at a process
that had never opened a socket. Nothing was red anywhere.
"""
import json
import subprocess
import sys
import urllib.request

import pytest
from django.test import override_settings

from stapel_core.observability import backends, metrics
from stapel_core.observability import celery as celery_hook
from stapel_core.observability import exporter

celery = pytest.importorskip("celery")


@pytest.fixture
def fresh_hook(monkeypatch):
    """A process that has not connected the Celery handlers yet."""
    from celery import signals

    monkeypatch.setattr(celery_hook, "_installed", False)
    yield celery_hook
    for signal in (signals.celeryd_init, signals.beat_init):
        signal.disconnect(dispatch_uid=celery_hook._UID)
    celery_hook._installed = False


@pytest.fixture
def no_listener(monkeypatch):
    """No exporter is running, and none is left running afterwards."""
    monkeypatch.setattr(exporter, "_server", None)
    yield
    server = exporter._server
    if server is not None:
        server.shutdown()
        server.server_close()
    exporter._server = None


def _scrape(server) -> str:
    port = server.server_address[1]
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/metrics", timeout=5
    ) as resp:
        return resp.read().decode()


# ── installing the hook ─────────────────────────────────────────────────


class TestTheHookInstallsItself:
    def test_a_process_that_never_imported_celery_pays_nothing(
        self, fresh_hook, monkeypatch
    ):
        """`ready()` runs in every web and manage.py process too. Connecting
        there would mean importing Celery for a signal that can never fire."""
        monkeypatch.delitem(sys.modules, "celery", raising=False)
        assert fresh_hook.install() is False
        assert fresh_hook.is_installed() is False

    def test_it_connects_where_celery_is_running(self, fresh_hook):
        from celery import signals

        assert fresh_hook.install() is True
        assert fresh_hook.is_installed() is True
        assert signals.celeryd_init.has_listeners()
        assert signals.beat_init.has_listeners()

    def test_connecting_twice_does_not_serve_twice(self, fresh_hook):
        assert fresh_hook.install() is True
        assert fresh_hook.install() is False

    def test_force_connects_without_celery_in_sys_modules(
        self, fresh_hook, monkeypatch
    ):
        """The documented one-liner for config/celery.py, where the import is
        obviously already happening."""
        monkeypatch.delitem(sys.modules, "celery", raising=False)
        assert fresh_hook.install(force=True) is True


# ── the port actually opens, and answers with the task's counter ────────


COUNTER = "test_celery_task_ran_total"


class TestAWorkerServesWhatItsTasksRecord:
    @override_settings(
        STAPEL_OBSERVABILITY={
            "EXPORTER_PORT": 0,  # any free port
            "EXPORTER_ADDR": "127.0.0.1",
            "METRIC_NAMESPACE": "",
        }
    )
    def test_celeryd_init_opens_the_port_and_a_task_shows_up_on_it(
        self, fresh_hook, no_listener
    ):
        from celery import Celery, signals

        metrics.reset_backend()
        app = Celery("stapel-core-test")
        app.conf.task_always_eager = True

        @app.task(name="stapel_core.tests.record")
        def record():
            metrics.counter(COUNTER, 1.0, description="tasks that ran")

        fresh_hook.install()
        # What `celery -A config worker` sends once the worker process is up.
        signals.celeryd_init.send(
            sender="worker@test",
            instance=None,
            conf=app.conf,
            options={"pool": "solo", "concurrency": 1},
        )

        assert exporter._server is not None, (
            "celeryd_init did not open the listener EXPORTER_PORT asked for"
        )

        record.apply()

        body = _scrape(exporter._server)
        assert COUNTER in body, body

    @override_settings(STAPEL_OBSERVABILITY={"LOG_FORMAT": "json"})
    def test_no_port_configured_means_no_port_opened(
        self, fresh_hook, no_listener
    ):
        """A worker that starts listening on a port nobody asked for is a
        surprise, and in some deployments a security finding."""
        from celery import Celery, signals

        app = Celery("stapel-core-test")
        fresh_hook.install()
        signals.celeryd_init.send(
            sender="worker@test", instance=None, conf=app.conf, options={}
        )
        assert exporter._server is None

    @override_settings(
        STAPEL_OBSERVABILITY={
            "EXPORTER_PORT": 0,
            "EXPORTER_ADDR": "127.0.0.1",
        }
    )
    def test_beat_gets_the_same_listener(self, fresh_hook, no_listener):
        from celery import signals

        fresh_hook.install()
        signals.beat_init.send(sender=object())
        assert exporter._server is not None


# ── the prefork blind spot is announced, not discovered in production ───


class TestThePreforkBlindSpotIsAnnounced:
    """Counters recorded in a forked child are invisible to the parent's
    registry — the port is up, the scrape succeeds, the numbers are missing.
    That is the original defect wearing a listener."""

    @override_settings(STAPEL_OBSERVABILITY={"EXPORTER_PORT": 9102})
    def test_prefork_with_concurrency_and_no_multiproc_dir_is_named(
        self, monkeypatch
    ):
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
        monkeypatch.delenv("prometheus_multiproc_dir", raising=False)
        warning = celery_hook._prefork_blind_spot(
            None, {"pool": "prefork", "concurrency": 4}
        )
        assert warning is not None
        assert "PROMETHEUS_MULTIPROC_DIR" in warning
        assert "--pool=solo" in warning

    @override_settings(STAPEL_OBSERVABILITY={"EXPORTER_PORT": 9102})
    def test_celery_default_concurrency_counts_as_concurrent(self, monkeypatch):
        """`--concurrency` unset means one child per CPU, which is more than
        one anywhere that matters — silence there would be the whole defect."""
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
        assert celery_hook._prefork_blind_spot(None, {"pool": "prefork"})

    @override_settings(STAPEL_OBSERVABILITY={"EXPORTER_PORT": 9102})
    def test_a_pool_that_runs_tasks_in_this_process_is_silent(
        self, monkeypatch
    ):
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
        assert celery_hook._prefork_blind_spot(None, {"pool": "solo"}) is None
        assert celery_hook._prefork_blind_spot(None, {"pool": "threads"}) is None

    @override_settings(STAPEL_OBSERVABILITY={"EXPORTER_PORT": 9102})
    def test_multiprocess_mode_answers_it(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
        assert celery_hook._prefork_blind_spot(
            None, {"pool": "prefork", "concurrency": 8}
        ) is None

    @override_settings(STAPEL_OBSERVABILITY={"LOG_FORMAT": "json"})
    def test_nothing_is_said_when_no_port_was_asked_for(self, monkeypatch):
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
        assert celery_hook._prefork_blind_spot(
            None, {"pool": "prefork", "concurrency": 8}
        ) is None


# ── multiprocess mode: the parent reads what a child recorded ───────────


_CHILD = """
import os, sys
os.environ["PROMETHEUS_MULTIPROC_DIR"] = {dir!r}
import django
from django.conf import settings
settings.configure(
    SECRET_KEY="x",
    INSTALLED_APPS=[],
    DATABASES={{}},
    STAPEL_OBSERVABILITY={{"METRIC_NAMESPACE": ""}},
)
django.setup()
from stapel_core.observability import metrics
metrics.counter({metric!r}, 3.0, description="recorded in the child")
"""


class TestTheParentReadsAForkedChildsCounters:
    """`PROMETHEUS_MULTIPROC_DIR` is the answer for a prefork worker, and it
    only counts if the process that serves the scrape can see numbers it did
    not record itself."""

    def test_expose_collects_the_whole_directory(self, tmp_path, monkeypatch):
        metric = "multiproc_child_total"
        script = tmp_path / "child.py"
        script.write_text(
            _CHILD.format(dir=str(tmp_path), metric=metric)
        )
        # A separate process, not a thread: shared mmap files are the only
        # thing being tested and a thread would share the registry too.
        result = subprocess.run(
            [sys.executable, "child.py"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr

        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
        # A backend that has recorded nothing at all: everything it answers
        # with came out of the directory.
        backend = backends.PrometheusMetricsBackend()
        body = backend.expose()
        sample = [
            line for line in body.splitlines()
            if line.startswith(metric) and not line.startswith("#")
        ]
        assert sample, body
        assert sample[0].endswith(" 3.0"), sample

    def test_a_broken_directory_costs_only_the_children(
        self, tmp_path, monkeypatch, caplog
    ):
        """Falling back to this process's own registry beats answering the
        scrape with nothing at all."""
        import logging

        monkeypatch.setenv(
            "PROMETHEUS_MULTIPROC_DIR", str(tmp_path / "does-not-exist")
        )
        backend = backends.PrometheusMetricsBackend()
        with caplog.at_level(logging.WARNING, logger=backends.__name__):
            body = backend.expose()
        assert any(
            "PROMETHEUS_MULTIPROC_DIR" in r.message for r in caplog.records
        )
        # The local registry answered instead — python_info is always there.
        assert body != ""


# ── W005: a port set on a process that opens no port ────────────────────


class TestW005:
    def _run(self, argv, monkeypatch):
        from stapel_core.observability import checks

        monkeypatch.setattr(sys, "argv", argv)
        return [w.id for w in checks.check_exporter_port_is_served()]

    @override_settings(STAPEL_OBSERVABILITY={"EXPORTER_PORT": 9102})
    def test_a_command_with_no_listener_is_named(self, monkeypatch):
        from stapel_core.observability import checks

        ids = self._run(["manage.py", "sweep_tasks"], monkeypatch)
        assert checks.W005_EXPORTER_NEVER_SERVED in ids

    @override_settings(STAPEL_OBSERVABILITY={"EXPORTER_PORT": 9102})
    def test_a_command_that_serves_the_port_is_not(self, monkeypatch):
        assert self._run(["manage.py", "dispatch_outbox"], monkeypatch) == []

    @override_settings(STAPEL_OBSERVABILITY={"EXPORTER_PORT": 9102})
    def test_djangos_own_commands_are_exempt_wholesale(self, monkeypatch):
        """`manage.py check` runs this check. Firing on it would put the
        warning in front of every boot gate in the fleet."""
        assert self._run(["manage.py", "check"], monkeypatch) == []
        assert self._run(["manage.py", "migrate"], monkeypatch) == []
        assert self._run(["manage.py", "runserver"], monkeypatch) == []

    @override_settings(STAPEL_OBSERVABILITY={"EXPORTER_PORT": 9102})
    def test_a_celery_or_wsgi_process_is_not_a_management_command(
        self, monkeypatch
    ):
        assert self._run(["celery", "-A", "config", "worker"], monkeypatch) == []
        assert self._run(["gunicorn", "config.wsgi"], monkeypatch) == []
        assert self._run(["manage.py"], monkeypatch) == []

    @override_settings(STAPEL_OBSERVABILITY={"LOG_FORMAT": "json"})
    def test_silent_when_no_port_was_configured(self, monkeypatch):
        assert self._run(["manage.py", "sweep_tasks"], monkeypatch) == []

    def test_a_service_that_never_adopted_the_facade_is_not_nagged(
        self, monkeypatch
    ):
        from django.conf import settings

        with override_settings():
            if hasattr(settings, "STAPEL_OBSERVABILITY"):
                delattr(settings._wrapped, "STAPEL_OBSERVABILITY")
            assert self._run(["manage.py", "sweep_tasks"], monkeypatch) == []

    def test_every_serving_command_declares_it(self):
        """The marker is the contract W005 reads. A command that calls
        serve_metrics() and forgets it gets warned about by name."""
        from stapel_core.bus.consumer import BaseBusConsumerCommand
        from stapel_core.django.management.commands.serve_functions import (
            Command as ServeFunctions,
        )
        from stapel_core.django.outbox.management.commands.dispatch_outbox import (
            Command as DispatchOutbox,
        )

        for command in (BaseBusConsumerCommand, ServeFunctions, DispatchOutbox):
            assert command.stapel_serves_metrics is True, command


# ── the zero-config claim, end to end ───────────────────────────────────


_WORKER = """
import json, os, sys, urllib.request
# The Celery CLI is the entry point, so the package is in sys.modules long
# before Django is set up. That ordering is what install() relies on.
import celery
from celery import Celery, signals

import django
from django.conf import settings
settings.configure(
    SECRET_KEY="x",
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3",
                            "NAME": ":memory:"}},
    INSTALLED_APPS=["django.contrib.contenttypes", "django.contrib.auth",
                    "rest_framework", "stapel_core.django"],
    MIDDLEWARE=[],
    ROOT_URLCONF="",
    USE_TZ=True,
    STAPEL_OBSERVABILITY={"EXPORTER_PORT": 0, "EXPORTER_ADDR": "127.0.0.1",
                          "METRIC_NAMESPACE": ""},
)
django.setup()   # AppConfig.ready() runs here — no service code involved

from stapel_core.observability import celery as hook, exporter, metrics
assert hook.is_installed(), "ready() did not install the celery hook"

app = Celery("probe")
app.conf.task_always_eager = True

@app.task(name="probe.record")
def record():
    metrics.counter("probe_task_total", 1.0, description="probe")

signals.celeryd_init.send(sender="worker@probe", instance=None,
                          conf=app.conf, options={"pool": "solo",
                                                  "concurrency": 1})
assert exporter._server is not None, "no listener after celeryd_init"
record.apply()
port = exporter._server.server_address[1]
with urllib.request.urlopen("http://127.0.0.1:%d/metrics" % port, timeout=5) as r:
    body = r.read().decode()
print(json.dumps({"port": port, "has_counter": "probe_task_total" in body}))
"""


def test_a_fleet_service_needs_no_code_at_all(tmp_path):
    """The whole claim of this release: `stapel_core.django` in INSTALLED_APPS
    and `EXPORTER_PORT` in the environment, nothing else, and the worker is
    scrapable."""
    script = tmp_path / "worker.py"
    script.write_text(_WORKER)
    result = subprocess.run(
        [sys.executable, "worker.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["has_counter"] is True
    assert payload["port"] > 0
