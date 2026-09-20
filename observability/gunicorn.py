"""The gunicorn hooks multiprocess metrics need, as a config module.

Point gunicorn at it and the whole of the web half is wired::

    gunicorn config.wsgi:application -c python:stapel_core.observability.gunicorn

or, for a deployment that already has a config file, compose it::

    # gunicorn.conf.py
    from stapel_core.observability.gunicorn import child_exit, on_starting  # noqa: F401

    workers = 4
    bind = "0.0.0.0:8000"

Two hooks, and neither is optional if ``PROMETHEUS_MULTIPROC_DIR`` is set:

``on_starting``
    Runs in the master, before a single worker forks — the only moment at
    which the directory can be wiped without racing a worker that is already
    writing into it. Stale ``.db`` files from the previous run of the same
    container are adopted by the new workers' pids; see
    :func:`stapel_core.observability.multiprocess.prepare_multiprocess_dir`.

``child_exit``
    Runs in the master when a worker dies — a crash, a timeout kill, or an
    ordinary ``--max-requests`` recycle. Without it the dead worker's
    live-mode gauge files stay in the directory and keep being aggregated,
    so a flag that a worker last set to 1 outlives the worker and the alert
    over it can never clear.

``post_fork`` is deliberately absent. A forked worker needs no metrics
setup: ``prometheus_client`` already decided its value class from the
environment at import time, which is exactly why the variable belongs in
the container environment and not in Python.

With the environment variable unset every hook here is a no-op, so this
module is safe to name unconditionally in an image's default gunicorn
config — a single-worker deployment behaves exactly as it did before.
"""
from __future__ import annotations

import logging

from .multiprocess import mark_process_dead, prepare_multiprocess_dir

logger = logging.getLogger(__name__)

__all__ = ["on_starting", "child_exit", "worker_exit", "install"]


def on_starting(server=None) -> None:
    """Master boot: give this container an empty multiprocess directory."""
    path = prepare_multiprocess_dir()
    if path:
        logger.info(
            "stapel_core.observability: multiprocess metrics directory %s is "
            "ready; every worker's numbers will be aggregated on one scrape",
            path,
        )


def child_exit(server=None, worker=None) -> None:
    """A worker died: stop counting its live-mode gauges.

    Runs in the MASTER (gunicorn calls ``child_exit`` there, ``worker_exit``
    in the worker itself). The master is the process that still exists after
    the worker is gone, so it is the one that can clean up after it.
    """
    pid = getattr(worker, "pid", None)
    if pid is None:
        return
    mark_process_dead(pid)


def worker_exit(server=None, worker=None) -> None:
    """A worker is shutting itself down cleanly.

    Named for the deployments whose config already defines ``child_exit``
    and cannot take another: hooking this one instead still retires the
    files, just from inside the worker and only on a graceful exit. A
    worker killed for a timeout never reaches it — which is why
    :func:`child_exit` is the one this module documents first.
    """
    pid = getattr(worker, "pid", None)
    mark_process_dead(pid)


def install(namespace: dict) -> None:
    """Merge these hooks into a gunicorn config module's namespace.

    For a config file that wants the hooks without naming each one::

        from stapel_core.observability.gunicorn import install
        install(globals())

    Only hooks the config has not already defined are added: a deployment
    that wrote its own ``child_exit`` keeps it, and is responsible for
    calling :func:`stapel_core.observability.multiprocess.mark_process_dead`
    from it.
    """
    for name, hook in (("on_starting", on_starting), ("child_exit", child_exit)):
        if name not in namespace:
            namespace[name] = hook
