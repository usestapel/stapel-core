"""stapel_core.background: a job is a celery task with celery, inline without."""
import subprocess
import sys

import pytest

from stapel_core import background
from stapel_core.background import InlineTask, background_task


def _no_celery(monkeypatch):
    monkeypatch.setattr(background, "_celery_shared_task", lambda: None)


def test_without_celery_the_job_is_inline_and_runs_now(monkeypatch):
    _no_celery(monkeypatch)
    calls = []

    @background_task()
    def job(a, b=0):
        calls.append((a, b))
        return a + b

    assert isinstance(job, InlineTask)
    assert job.name == f"{__name__}.job"
    assert job.delay(1, b=2) == 3
    assert job.apply_async(args=(4,), kwargs={"b": 5}, queue="q", countdown=9) == 9
    assert job(6) == 6
    assert calls == [(1, 2), (4, 5), (6, 0)]
    assert job.__name__ == "job"


def test_without_celery_an_explicit_name_is_kept(monkeypatch):
    _no_celery(monkeypatch)

    @background_task(name="lib.tasks.renamed", queue="thumbnails")
    def job():
        return "ok"

    assert job.name == "lib.tasks.renamed"
    assert job.delay() == "ok"


def test_without_celery_bind_passes_the_task_and_retry_raises(monkeypatch):
    _no_celery(monkeypatch)

    @background_task(bind=True, max_retries=3)
    def job(self, value):
        if value < 0:
            raise self.retry(exc=ValueError(value))
        return (self.name, value)

    assert job.delay(1) == (f"{__name__}.job", 1)
    with pytest.raises(ValueError):
        job.delay(-1)
    with pytest.raises(RuntimeError):
        job.retry()


def test_with_celery_the_job_is_a_shared_task_under_the_same_name():
    pytest.importorskip("celery")

    @background_task(bind=True, max_retries=2)
    def job(self, x):
        return x

    assert not isinstance(job, InlineTask)
    assert job.name == f"{__name__}.job"
    assert job.max_retries == 2
    assert hasattr(job, "delay") and hasattr(job, "apply_async")


def test_a_process_that_cannot_import_celery_gets_inline_jobs():
    # The real probe, not the monkeypatched one: celery made unimportable.
    code = (
        "import sys; sys.modules['celery'] = None\n"
        "from stapel_core.background import background_task, InlineTask\n"
        "@background_task()\n"
        "def job(x): return x * 2\n"
        "assert isinstance(job, InlineTask), job\n"
        "assert job.delay(21) == 42\n"
        "print('inline-ok')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "inline-ok" in out.stdout
