"""Make a multi-worker process model report ONE set of numbers.

THE DEFECT, MEASURED ON A LIVE HOST
    Services run their web container as ``gunicorn --workers 2``. Every
    worker is a separate OS process, ``prometheus_client`` keeps its values
    in that process's memory, and a scrape reaches exactly one of them —
    whichever the socket handed the connection to. A gauge set in worker A
    (a provider "out of credits" flag) was visible only on the scrapes that
    happened to land on A: an instant query answered EMPTY while a
    twenty-minute range showed samples of 1. Counters undercount by the
    share of scrapes that miss the worker that owns them, gauges flap
    between their value and absence, and any alert rule written over an
    application metric is a coin flip. Celery's prefork pool has the same
    split: the task bodies run in children, the exporter runs in the parent.

    This is not a dashboard problem. It is the difference between "the
    provider is refusing and we were paged" and "the provider is refusing
    and the query returned nothing".

THE MECHANISM
    ``prometheus_client``'s multiprocess mode: with
    ``PROMETHEUS_MULTIPROC_DIR`` set in the process ENVIRONMENT, every value
    is backed by an mmap'd file in that directory instead of by process
    memory, and any process can collect the whole set with a
    ``MultiProcessCollector``. The scrape then reports the aggregate across
    every worker, whichever worker serves it.

    Four things have to be true for that to be worth switching on, and each
    one has a function here:

    1. **The directory is per container and EMPTY at start.**
       :func:`prepare_multiprocess_dir`. The files are named by pid, and a
       container that restarts hands out the same small pids again — so a
       leftover ``counter_9.db`` from the previous run is read as the new
       pid 9's counter and the series jumps backwards or forwards by a
       whole run's worth. A tmpfs mount plus this wipe makes "stale file"
       unrepresentable.
    2. **A worker that exits stops being counted.**
       :func:`mark_process_dead`, called from gunicorn's ``child_exit`` and
       Celery's ``worker_process_shutdown`` — see
       :mod:`stapel_core.observability.gunicorn` and
       :mod:`stapel_core.observability.celery`. Without it a recycled worker
       keeps contributing its last gauge value forever, which is how a
       cleared alert stays lit.
    3. **Every gauge declares how its per-process values combine.**
       ``multiprocess_mode=`` on the facade's :func:`~.metrics.gauge`. The
       library default is ``all``, which emits one series PER PID — a
       dashboard panel that was one line becomes N lines named by a pid that
       means nothing, and ``max()`` over it silently includes dead workers.
    4. **The env being unset changes nothing.** Everything here answers
       "not in multiprocess mode" and returns without touching anything, so
       a single-process deployment behaves byte-identically to before.

WHY THE ENVIRONMENT AND NOT A SETTING
    ``prometheus_client`` picks its value class when ``prometheus_client.values``
    is first imported. A value assigned from Python — in ``settings.py``, in
    ``AppConfig.ready()`` — is already too late for any metric declared at
    import time, and "too late" here means half the process's metrics are
    mmap-backed and half are not. So the container's environment says it, an
    entrypoint prepares the directory, and this module only ever READS the
    variable.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

__all__ = [
    "MULTIPROC_ENV",
    "multiprocess_dir",
    "prepare_multiprocess_dir",
    "mark_process_dead",
    "process_model_workers",
]

#: ``prometheus_client`` renamed this in 0.4 and still reads both spellings,
#: so both are read here — otherwise this module and the library could
#: disagree about whether the process is in multiprocess mode.
MULTIPROC_ENV = ("PROMETHEUS_MULTIPROC_DIR", "prometheus_multiproc_dir")


def multiprocess_dir() -> str | None:
    """The ``PROMETHEUS_MULTIPROC_DIR`` this process runs under, if any."""
    for name in MULTIPROC_ENV:
        value = os.environ.get(name)
        if value:
            return value
    return None


def prepare_multiprocess_dir(path: str | None = None) -> str | None:
    """Create the multiprocess directory and remove every file in it.

    Called ONCE per container, before any worker forks — from the
    entrypoint, or from gunicorn's ``on_starting`` (see
    :mod:`stapel_core.observability.gunicorn`). Returns the directory when
    it is ready to be written to, None when there is nothing to do (the env
    is unset) or it could not be prepared.

    The wipe is the point. ``prometheus_client`` names its files after the
    pid that owns them; a container restart reuses low pids, so a ``.db``
    left by the previous run is silently adopted by an unrelated new worker.
    The result is a counter that appears to have decreased (Prometheus reads
    that as a reset and the rate spikes) or a gauge stuck at a value nothing
    in this run ever set.

    Never raises: a metrics directory is an observation of the work, and a
    container must not fail to start over one. A directory that could not be
    cleaned is reported at ERROR — leaving stale files in place would be
    worse than the numbers being absent, because wrong numbers are believed.
    """
    path = path or multiprocess_dir()
    if not path:
        return None
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        logger.error(
            "stapel_core.observability: PROMETHEUS_MULTIPROC_DIR=%s could not "
            "be created; this container's workers will each keep their own "
            "metrics and a scrape will report only one of them",
            path, exc_info=True,
        )
        return None

    removed = 0
    try:
        for entry in os.listdir(path):
            if not entry.endswith(".db"):
                continue
            try:
                os.remove(os.path.join(path, entry))
                removed += 1
            except OSError:
                logger.error(
                    "stapel_core.observability: stale metrics file %s in %s "
                    "could not be removed; it will be read as if a worker of "
                    "THIS run had written it",
                    entry, path, exc_info=True,
                )
    except OSError:
        logger.error(
            "stapel_core.observability: PROMETHEUS_MULTIPROC_DIR=%s could not "
            "be listed", path, exc_info=True,
        )
        return None

    if removed:
        logger.info(
            "stapel_core.observability: removed %d stale metric file(s) from "
            "%s", removed, path,
        )
    return path


def mark_process_dead(pid: int | None = None, path: str | None = None) -> bool:
    """Retire the live-mode gauge files of a worker that has exited.

    Returns whether the bookkeeping ran. Without it, the ``live*``
    multiprocess modes cannot do their job: a gunicorn worker recycled by
    ``--max-requests`` leaves its last gauge value in the directory, and
    ``livemax`` over an out-of-credits flag keeps reporting 1 long after
    every living worker is being served again — an alert that can never
    clear, which is the same class of lie as an alert that never fires.

    Counters and histograms are deliberately NOT removed by
    ``prometheus_client`` here: they are cumulative, and a counter that
    forgets the work a dead worker did would make a rate go backwards.

    Never raises. Called from a signal handler and from gunicorn's
    ``child_exit``, neither of which may be taken down by a metrics chore.
    """
    path = path or multiprocess_dir()
    if not path:
        return False
    pid = os.getpid() if pid is None else pid
    try:
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(str(pid), path)
        return True
    except Exception:
        logger.warning(
            "stapel_core.observability: could not retire the metric files of "
            "pid %s in %s; a live-mode gauge may keep reporting its last "
            "value", pid, path, exc_info=True,
        )
        return False


# ─── "is this process model multi-worker?" ────────────────────────────────
#
# Read for check W006 only. It answers from the two things that are true
# before any worker forks: the environment a container was given, and the
# command line it was started with.

_WORKER_ENV = ("GUNICORN_WORKERS", "WEB_CONCURRENCY", "GUNICORN_CMD_ARGS")
_CELERY_CONCURRENCY_ENV = (
    "CELERY_WORKER_CONCURRENCY", "CELERYD_CONCURRENCY",
)
_PREFORK_POOLS = frozenset({"prefork", "processes"})


def _int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _argv_value(argv, *flags) -> str | None:
    """``--workers 4``, ``--workers=4`` and ``-c 4``, from a copy of argv."""
    for i, arg in enumerate(argv):
        for flag in flags:
            if arg == flag and i + 1 < len(argv):
                return argv[i + 1]
            if arg.startswith(flag + "="):
                return arg.split("=", 1)[1]
    return None


def process_model_workers() -> tuple[str, int] | None:
    """``(process model, worker count)`` when this process forks workers.

    None when it does not, or when it cannot be told — the honest answer for
    a ``manage.py`` command, a test run, or a gunicorn started from a config
    file this function cannot see. A check that guesses in the noisy
    direction is a check people learn to scroll past, so silence is the
    default and only positive evidence speaks.
    """
    import sys

    argv = [str(a) for a in (getattr(sys, "argv", ()) or ())]
    program = os.path.basename(argv[0]) if argv else ""

    if "gunicorn" in program or "gunicorn" in " ".join(argv[:2]):
        count = _int(_argv_value(argv, "--workers", "-w"))
        if count is None:
            for name in ("GUNICORN_WORKERS", "WEB_CONCURRENCY"):
                count = _int(os.environ.get(name))
                if count is not None:
                    break
        if count is None:
            cmd_args = os.environ.get("GUNICORN_CMD_ARGS") or ""
            count = _int(_argv_value(cmd_args.split(), "--workers", "-w"))
        if count is not None and count > 1:
            return ("gunicorn", count)
        return None

    if "celery" in program or "celery" in argv[:2]:
        if "worker" not in argv:
            return None
        pool = _argv_value(argv, "--pool", "-P") or os.environ.get(
            "CELERY_WORKER_POOL", "prefork"
        )
        if str(pool).rsplit(".", 1)[-1].lower() not in _PREFORK_POOLS:
            return None
        raw = _argv_value(argv, "--concurrency", "-c")
        if raw is None:
            for name in _CELERY_CONCURRENCY_ENV:
                raw = os.environ.get(name)
                if raw:
                    break
        count = _int(raw)
        if count is None:
            # Celery's default is one child per CPU: more than one anywhere
            # this matters, and a container pinned to a single CPU is not a
            # reason to stay quiet about the ones that are not.
            return ("celery prefork", os.cpu_count() or 2)
        if count > 1:
            return ("celery prefork", count)
        return None

    # Environment alone, for a process started through a wrapper this cannot
    # recognise (an entrypoint that execs gunicorn under another name).
    for name in ("GUNICORN_WORKERS", "WEB_CONCURRENCY"):
        count = _int(os.environ.get(name))
        if count is not None and count > 1:
            return ("web", count)
    return None
