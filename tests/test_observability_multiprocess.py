"""Several worker processes, one set of numbers.

The defect these cover, measured on a live host: a service running
``gunicorn --workers 2`` set a gauge in one worker, and an instant query for
it came back EMPTY while a twenty-minute range showed samples of 1 — the
scrape reached whichever worker the socket picked. Everything here is about
that: an aggregate that does not depend on which process answered, a dead
worker that stops voting, and an unset environment variable that changes
nothing at all.

Real forks, not mocks. ``prometheus_client`` chooses its value class at
import time from the environment, so the only honest test of multiprocess
mode is a child process that writes into the directory and a parent that
reads it back.
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest

from stapel_core.observability import multiprocess as mp


#: The repo root holds a `django/` package directory (the flat layout puts
#: stapel_core's own django subpackage there), so a child interpreter that
#: inherits it on PYTHONPATH imports THAT instead of the framework — the same
#: shadowing tests/conftest.py strips from sys.path.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _child_path() -> str:
    return os.pathsep.join(
        p for p in sys.path
        if p and os.path.abspath(p) != _REPO_ROOT
    )


def _child_kwargs(env, cwd) -> dict:
    """`python -c` also prepends the CWD, which is the repo root under pytest.

    So the child is run from somewhere else entirely; otherwise its
    `import django` finds stapel_core's own django/ directory and the test
    fails for a reason that has nothing to do with metrics.
    """
    return dict(
        env=env, cwd=str(cwd), capture_output=True, text=True, timeout=120,
    )


def _run_child(tmp_path, body: str, env_extra=None) -> str:
    """Run *body* in a fresh interpreter under PROMETHEUS_MULTIPROC_DIR."""
    env = dict(os.environ)
    env["PROMETHEUS_MULTIPROC_DIR"] = str(tmp_path)
    env["PYTHONPATH"] = _child_path()
    env.update(env_extra or {})
    script = textwrap.dedent(body)
    proc = subprocess.run(
        [sys.executable, "-c", script], **_child_kwargs(env, tmp_path)
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


_CHILD = """
    import os
    from prometheus_client import Counter, Gauge
    c = Counter("t_mp_jobs_total", "jobs")
    g = Gauge("t_mp_flag", "flag", multiprocess_mode="livemax")
    s = Gauge("t_mp_share", "share", multiprocess_mode="livesum")
    c.inc({inc})
    g.set({flag})
    s.set({share})
    print(os.getpid())
"""


@pytest.fixture
def multiproc_dir(tmp_path, monkeypatch):
    pytest.importorskip("prometheus_client")
    d = tmp_path / "prom"
    d.mkdir()
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(d))
    return d


def _collect(path) -> str:
    from prometheus_client import CollectorRegistry, generate_latest, multiprocess

    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry, path=str(path))
    return generate_latest(registry).decode()


def _series(text, name):
    out = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        series, _, value = line.rpartition(" ")
        if series.split("{")[0] == name:
            out[series] = float(value)
    return out


class TestTwoProcessesOneScrape:
    """RED before the mechanism: each process answered only for itself."""

    def test_counters_from_two_processes_add_up(self, multiproc_dir):
        _run_child(multiproc_dir, _CHILD.format(inc=2, flag=0, share=3))
        _run_child(multiproc_dir, _CHILD.format(inc=5, flag=0, share=4))
        text = _collect(multiproc_dir)
        assert _series(text, "t_mp_jobs_total") == {"t_mp_jobs_total": 7.0}

    def test_a_flag_raised_in_one_process_is_on_every_scrape(self, multiproc_dir):
        """The live incident: worker A set 1, an instant query saw nothing."""
        _run_child(multiproc_dir, _CHILD.format(inc=1, flag=1, share=0))
        _run_child(multiproc_dir, _CHILD.format(inc=1, flag=0, share=0))
        text = _collect(multiproc_dir)
        # livemax: ONE series, not one per pid, and it carries the 1 that
        # only one of the two processes ever wrote.
        assert _series(text, "t_mp_flag") == {"t_mp_flag": 1.0}

    def test_a_shared_quantity_is_the_sum_of_the_workers(self, multiproc_dir):
        _run_child(multiproc_dir, _CHILD.format(inc=1, flag=0, share=3))
        _run_child(multiproc_dir, _CHILD.format(inc=1, flag=0, share=4))
        assert _series(_collect(multiproc_dir), "t_mp_share") == {
            "t_mp_share": 7.0
        }

    def test_the_default_gauge_mode_is_one_series_not_one_per_pid(
        self, multiproc_dir
    ):
        """A gauge whose call site names no mode must still be ONE line.

        prometheus_client's own default is ``all`` — a series per pid,
        labelled with a number that means nothing and changes on every
        worker recycle. The facade's default is not that.
        """
        from stapel_core.observability.backends import PrometheusMetricsBackend

        body = """
            from stapel_core.observability.backends import PrometheusMetricsBackend
            from prometheus_client import CollectorRegistry
            b = PrometheusMetricsBackend(registry=CollectorRegistry())
            b.gauge("t_mp_default", {value})
            print("ok")
        """
        _run_child(multiproc_dir, body.format(value=1))
        _run_child(multiproc_dir, body.format(value=2))
        series = _series(_collect(multiproc_dir), "t_mp_default")
        assert len(series) == 1, series
        assert "pid=" not in next(iter(series))
        assert PrometheusMetricsBackend  # imported for the child's sake

    def test_the_library_default_would_have_been_one_series_per_pid(
        self, multiproc_dir
    ):
        """What the previous line is protecting against, demonstrated.

        This is the RED of the test above: declare the gauge the way
        ``prometheus_client`` declares it when nobody says otherwise, and two
        workers produce two series labelled by a pid — a dashboard panel that
        multiplies by the worker count and a label that changes on every
        recycle.
        """
        body = """
            from prometheus_client import Gauge
            Gauge("t_mp_library_default", "d").set({value})
            print("ok")
        """
        _run_child(multiproc_dir, body.format(value=1))
        _run_child(multiproc_dir, body.format(value=2))
        series = _series(_collect(multiproc_dir), "t_mp_library_default")
        assert len(series) == 2, series
        assert all("pid=" in s for s in series)


class TestDeadWorkersStopVoting:
    def test_mark_process_dead_removes_a_live_gauge(self, multiproc_dir):
        pid = _run_child(
            multiproc_dir, _CHILD.format(inc=1, flag=1, share=5)
        ).strip()
        assert _series(_collect(multiproc_dir), "t_mp_flag") == {"t_mp_flag": 1.0}

        assert mp.mark_process_dead(int(pid)) is True

        # The flag and the share are gone with the worker that held them;
        # the counter stays, because a rate must never go backwards.
        assert _series(_collect(multiproc_dir), "t_mp_flag") == {}
        assert _series(_collect(multiproc_dir), "t_mp_share") == {}
        assert _series(_collect(multiproc_dir), "t_mp_jobs_total") == {
            "t_mp_jobs_total": 1.0
        }

    def test_mark_process_dead_is_a_no_op_without_the_env(self, monkeypatch):
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
        monkeypatch.delenv("prometheus_multiproc_dir", raising=False)
        assert mp.mark_process_dead(1) is False

    def test_gunicorn_child_exit_retires_the_worker(self, multiproc_dir):
        from stapel_core.observability import gunicorn as hooks

        pid = _run_child(
            multiproc_dir, _CHILD.format(inc=1, flag=1, share=1)
        ).strip()

        class _Worker:
            pass

        worker = _Worker()
        worker.pid = int(pid)
        hooks.child_exit(server=None, worker=worker)
        assert _series(_collect(multiproc_dir), "t_mp_flag") == {}

    def test_child_exit_without_a_worker_does_nothing(self, multiproc_dir):
        from stapel_core.observability import gunicorn as hooks

        hooks.child_exit(server=None, worker=None)  # must not raise


class TestStaleFilesFromThePreviousRun:
    def test_prepare_wipes_the_directory(self, multiproc_dir):
        _run_child(multiproc_dir, _CHILD.format(inc=9, flag=1, share=9))
        assert _series(_collect(multiproc_dir), "t_mp_jobs_total")

        assert mp.prepare_multiprocess_dir() == str(multiproc_dir)

        assert _series(_collect(multiproc_dir), "t_mp_jobs_total") == {}
        assert list(multiproc_dir.glob("*.db")) == []

    def test_prepare_creates_a_missing_directory(self, tmp_path, monkeypatch):
        target = tmp_path / "not-yet"
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(target))
        assert mp.prepare_multiprocess_dir() == str(target)
        assert target.is_dir()

    def test_prepare_without_the_env_does_nothing(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
        monkeypatch.delenv("prometheus_multiproc_dir", raising=False)
        assert mp.prepare_multiprocess_dir() is None

    def test_gunicorn_on_starting_prepares(self, multiproc_dir):
        from stapel_core.observability import gunicorn as hooks

        (multiproc_dir / "counter_99999.db").write_bytes(b"junk")
        hooks.on_starting(server=None)
        assert list(multiproc_dir.glob("*.db")) == []


class TestSingleProcessIsUnchanged:
    """Env unset: byte-identical to the behaviour before this existed."""

    def test_exposition_is_the_processs_own_registry(self, monkeypatch):
        pytest.importorskip("prometheus_client")
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
        monkeypatch.delenv("prometheus_multiproc_dir", raising=False)
        from prometheus_client import CollectorRegistry

        from stapel_core.observability.backends import PrometheusMetricsBackend

        backend = PrometheusMetricsBackend(registry=CollectorRegistry())
        backend.gauge("t_single_flag", 1, {"provider": "acme"})
        text = backend.expose()
        assert 't_single_flag{provider="acme"} 1.0' in text
        assert "pid=" not in text

    def test_a_declared_mode_changes_nothing_in_one_process(self, monkeypatch):
        pytest.importorskip("prometheus_client")
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
        from prometheus_client import CollectorRegistry

        from stapel_core.observability.backends import PrometheusMetricsBackend

        plain = PrometheusMetricsBackend(registry=CollectorRegistry())
        plain.gauge("t_same", 4, {"a": "b"})
        moded = PrometheusMetricsBackend(registry=CollectorRegistry())
        moded.gauge("t_same", 4, {"a": "b"}, multiprocess_mode="livemax")
        assert plain.expose() == moded.expose()

    def test_an_unknown_mode_is_logged_not_raised(self, monkeypatch, caplog):
        pytest.importorskip("prometheus_client")
        from prometheus_client import CollectorRegistry

        from stapel_core.observability.backends import PrometheusMetricsBackend

        backend = PrometheusMetricsBackend(registry=CollectorRegistry())
        with caplog.at_level("WARNING"):
            backend.gauge("t_typo", 1, multiprocess_mode="liveMAXIMUM")
        assert "t_typo" in backend.expose()
        assert any("multiprocess_mode" in r.getMessage() for r in caplog.records)

    def test_two_call_sites_disagreeing_about_a_mode_are_reported(
        self, monkeypatch, caplog
    ):
        pytest.importorskip("prometheus_client")
        from prometheus_client import CollectorRegistry

        from stapel_core.observability.backends import PrometheusMetricsBackend

        backend = PrometheusMetricsBackend(registry=CollectorRegistry())
        backend.gauge("t_two_minds", 1, multiprocess_mode="livesum")
        with caplog.at_level("WARNING"):
            backend.gauge("t_two_minds", 2, multiprocess_mode="livemax")
        assert any("livesum" in r.getMessage() for r in caplog.records)

    def test_a_backend_without_the_keyword_still_records(self, caplog):
        from stapel_core.observability import metrics
        from stapel_core.observability.backends import MetricsBackend

        seen = []

        class OldBackend(MetricsBackend):
            def gauge(self, name, value, labels=None, *, description=""):
                seen.append((name, value))

        metrics.set_backend(OldBackend())
        try:
            metrics.gauge("legacy_backend_gauge", 7, multiprocess_mode="livemax")
        finally:
            metrics.set_backend(None)
        assert seen == [("stapel_legacy_backend_gauge", 7)]


class TestProcessModelDetection:
    def test_gunicorn_workers_on_the_command_line(self, monkeypatch):
        monkeypatch.setattr(
            sys, "argv", ["/opt/venv/bin/gunicorn", "config.wsgi", "--workers", "2"]
        )
        assert mp.process_model_workers() == ("gunicorn", 2)

    def test_one_gunicorn_worker_is_not_a_finding(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["gunicorn", "config.wsgi", "-w", "1"])
        assert mp.process_model_workers() is None

    def test_web_concurrency_from_the_environment(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["gunicorn", "config.wsgi"])
        monkeypatch.setenv("WEB_CONCURRENCY", "4")
        assert mp.process_model_workers() == ("gunicorn", 4)

    def test_celery_prefork_concurrency(self, monkeypatch):
        monkeypatch.setattr(
            sys, "argv", ["celery", "worker", "-A", "config", "--concurrency=3"]
        )
        assert mp.process_model_workers() == ("celery prefork", 3)

    def test_celery_solo_pool_is_not_a_finding(self, monkeypatch):
        monkeypatch.setattr(
            sys, "argv", ["celery", "worker", "--pool=solo", "-c", "8"]
        )
        assert mp.process_model_workers() is None

    def test_a_management_command_says_nothing(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["manage.py", "migrate"])
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
        monkeypatch.delenv("GUNICORN_WORKERS", raising=False)
        assert mp.process_model_workers() is None


class TestCheckW006:
    def _run(self):
        from stapel_core.observability.checks import check_multiprocess_metrics

        return check_multiprocess_metrics()

    @pytest.fixture(autouse=True)
    def _adopted(self, settings):
        settings.STAPEL_OBSERVABILITY = {
            "METRICS_BACKEND":
                "stapel_core.observability.backends.PrometheusMetricsBackend",
        }
        from stapel_core.observability import metrics

        metrics.reset_backend()
        yield
        metrics.reset_backend()

    def test_fires_for_multi_worker_without_the_env(self, monkeypatch):
        pytest.importorskip("prometheus_client")
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
        monkeypatch.setattr(sys, "argv", ["gunicorn", "config.wsgi", "-w", "2"])
        found = self._run()
        assert [w.id for w in found] == ["stapel_core.observability.W006"]
        assert "2 gunicorn workers" in found[0].msg

    def test_silent_when_the_env_is_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
        monkeypatch.setattr(sys, "argv", ["gunicorn", "config.wsgi", "-w", "2"])
        assert self._run() == []

    def test_silent_for_a_single_worker(self, monkeypatch):
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
        monkeypatch.setattr(sys, "argv", ["gunicorn", "config.wsgi", "-w", "1"])
        assert self._run() == []

    def test_silent_for_a_statsd_deployment(self, monkeypatch, settings):
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
        monkeypatch.setattr(sys, "argv", ["gunicorn", "config.wsgi", "-w", "2"])
        settings.STAPEL_OBSERVABILITY = {
            "METRICS_BACKEND":
                "stapel_core.observability.backends.StatsdMetricsBackend",
        }
        from stapel_core.observability import metrics

        metrics.reset_backend()
        assert self._run() == []

    def test_silent_for_a_service_that_never_adopted_the_facade(
        self, monkeypatch, settings
    ):
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
        monkeypatch.setattr(sys, "argv", ["gunicorn", "config.wsgi", "-w", "2"])
        settings.STAPEL_OBSERVABILITY = {}
        assert self._run() == []


class TestMetricsEndpointSaysWhichItIs:
    def test_the_indicator_is_zero_without_the_env(self, monkeypatch, rf):
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
        from stapel_core.django.monitoring.health import prometheus_metrics

        body = prometheus_metrics(rf.get("/api/metrics/")).content.decode()
        assert "stapel_metrics_multiprocess{" in body
        assert body.count("stapel_metrics_multiprocess{") == 1
        line = [
            ln for ln in body.splitlines()
            if ln.startswith("stapel_metrics_multiprocess{")
        ][0]
        assert line.endswith(" 0")

    def test_the_indicator_is_one_with_the_env(self, monkeypatch, rf, tmp_path):
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
        from stapel_core.django.monitoring.health import prometheus_metrics

        body = prometheus_metrics(rf.get("/api/metrics/")).content.decode()
        line = [
            ln for ln in body.splitlines()
            if ln.startswith("stapel_metrics_multiprocess{")
        ][0]
        assert line.endswith(" 1")


# ─── the cross-process alert throttle ────────────────────────────────────


_THROTTLE_CHILD = """
    import django, json, os
    from django.conf import settings
    settings.configure(
        CACHES={{"default": {{
            "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
            "LOCATION": {cache!r},
        }}}},
        USE_TZ=True,
    )
    django.setup()
    from stapel_core.observability.throttle import claim_slot, slot_is_shared
    loud = [claim_slot("t-provider", 3600)[0] for _ in range(5)]
    print(json.dumps({{"loud": sum(loud), "shared": slot_is_shared()}}))
"""


class TestCrossProcessThrottle:
    def test_ten_refusals_across_two_workers_raise_one_alert(self, tmp_path):
        """RED with a module-level dict: two workers, two alerts."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        env = dict(os.environ)
        env["PYTHONPATH"] = _child_path()
        env.pop("PROMETHEUS_MULTIPROC_DIR", None)
        script = textwrap.dedent(
            _THROTTLE_CHILD.format(cache=str(cache_dir))
        )
        results = []
        for _ in range(2):
            proc = subprocess.run(
                [sys.executable, "-c", script], **_child_kwargs(env, tmp_path)
            )
            assert proc.returncode == 0, proc.stderr
            results.append(json.loads(proc.stdout.strip().splitlines()[-1]))

        assert all(r["shared"] for r in results)
        assert sum(r["loud"] for r in results) == 1, results

    def test_locmem_is_not_treated_as_shared(self):
        from stapel_core.observability import throttle

        # The suite's own cache is locmem: per-process, so the honest answer
        # is "not shared" and the fallback is the in-process slot.
        assert throttle.slot_is_shared() is False

    def test_the_process_local_fallback_still_throttles(self):
        from stapel_core.observability import throttle

        throttle.clear_slots()
        loud = [throttle.claim_slot("t-local", 3600) for _ in range(5)]
        assert [flag for flag, _ in loud] == [True, False, False, False, False]
        assert loud[-1][1] == 4

    def test_a_cleared_condition_re_arms_the_next_alert(self):
        from stapel_core.observability import throttle

        throttle.clear_slots()
        assert throttle.claim_slot("t-rearm", 3600)[0] is True
        assert throttle.claim_slot("t-rearm", 3600)[0] is False
        throttle.release_slot("t-rearm")
        assert throttle.claim_slot("t-rearm", 3600)[0] is True

    def test_a_zero_interval_disables_the_throttle(self):
        from stapel_core.observability import throttle

        assert all(throttle.claim_slot("t-off", 0)[0] for _ in range(3))

    def test_a_shared_cache_reports_the_suppressed_count(self, settings, tmp_path):
        from stapel_core.observability import throttle

        settings.CACHES = {
            "default": {
                "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
                "LOCATION": str(tmp_path / "c"),
            }
        }
        assert throttle.slot_is_shared() is True
        assert throttle.claim_slot("t-count", 3600) == (True, 0)
        for _ in range(3):
            throttle.claim_slot("t-count", 3600)
        throttle.release_slot("t-count")
        # The window was released, so the next one is loud and reports what
        # it stands for... after a fresh round of suppressed occurrences.
        assert throttle.claim_slot("t-count", 3600)[0] is True
