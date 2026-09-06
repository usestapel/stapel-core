"""Give a Celery worker the metrics port every other worker already gets.

``serve_metrics()`` was called from exactly two places in the framework —
``BaseBusConsumerCommand.handle()`` and ``manage.py serve_functions`` — so a
deployment that set ``STAPEL_OBSERVABILITY["EXPORTER_PORT"]`` on a Celery
container watched its scrape job go green-then-down and never asked why: the
process Prometheus was pointed at had opened no socket at all. Everything
recorded in a task body — ``comm_task_failed_total``, ``bus_dlq_total`` for a
given-up Task, a product's own ladder counters (stapel-moderation's
``screen_failed`` / ``case_dlq``) — incremented in a process nothing could
reach. A counter that cannot be scraped is indistinguishable from a counter
that never fires, which is precisely the outage it was added to report.

The fix is not a third call site. It is the same call hung off Celery's own
startup signals, installed by ``stapel_core.django``'s ``AppConfig.ready()``:

    celery -A config worker
      → Celery's Django fixup calls django.setup() from `import_modules`
      → AppConfig.ready() runs → install() connects the handlers
      → `celeryd_init` fires → serve_metrics()

``import_modules`` is sent from ``WorkController.__init__`` *before*
``on_before_init`` sends ``celeryd_init`` (celery/worker/worker.py), so the
handler is always connected in time. **A fleet service needs no per-process
code**: setting ``EXPORTER_PORT`` on the worker container is the whole of it.
A project that does not install the ``stapel_core.django`` app can put the
one documented line in ``config/celery.py`` instead::

    from stapel_core.observability.celery import install
    install(force=True)

Beat gets the same treatment through ``beat_init`` — it records no task
metrics today, but it is the same class of process (long-lived, no HTTP), and
a schedule that starts recording something should not need a second release.

**The prefork blind spot, stated once and loudly.** The listener runs in the
MAIN worker process. Under the default ``--pool=prefork`` with concurrency
greater than one, every task body runs in a forked child, and
``prometheus_client``'s default value class is plain per-process memory: the
parent's registry — the one this listener exposes — never sees those
increments. The port is up, the scrape succeeds, and the numbers are still
missing. Two supported ways out:

* ``--pool=solo`` (or ``--pool=threads``): task bodies run in the process
  that serves the port. The simplest answer, and the right one for a
  low-throughput fleet worker.
* ``PROMETHEUS_MULTIPROC_DIR=/var/run/prometheus`` (an existing, empty,
  writable directory): ``prometheus_client`` switches to mmap-backed values
  that survive the fork, and
  :meth:`~stapel_core.observability.backends.PrometheusMetricsBackend.expose`
  collects them through a ``MultiProcessCollector``. The variable must be set
  in the process *environment* — ``prometheus_client`` decides at import
  time, so a value assigned from Python after the first metric exists is too
  late.

A worker that is prefork, concurrent, and has neither is warned about at
startup, by name, because that combination is the original defect wearing a
listener.
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

__all__ = ["install", "is_installed"]

# One uid for both signals: Celery's dispatcher keys on (signal, uid), so
# reconnecting after a settings reload or a second ready() replaces rather
# than duplicates. weak=False because module-level functions referenced only
# by the dispatcher must not be collected.
_UID = "stapel_core.observability.celery"

_installed = False


def is_installed() -> bool:
    """Whether the startup handlers are connected in this process."""
    return _installed


def install(force: bool = False) -> bool:
    """Connect ``serve_metrics()`` to Celery's worker and beat startup.

    Idempotent, and never raises: a metrics port is an observation of the
    work, and a worker must not fail to boot over one.

    ``force=False`` (how ``AppConfig.ready()`` calls it) connects only when
    ``celery`` is *already imported* in this process. That is not a
    heuristic — under ``celery -A config worker`` the Celery CLI is the
    entry point, so the package is in ``sys.modules`` long before Django is
    set up. It keeps a plain web or ``manage.py`` process from paying for an
    import it will never use.

    ``force=True`` imports Celery and connects regardless — the shape for the
    one line in ``config/celery.py`` when ``stapel_core.django`` is not an
    installed app.

    Returns True when the connection happened here.
    """
    global _installed
    if _installed:
        return False
    if not force and "celery" not in sys.modules:
        return False
    try:
        from celery import signals
    except Exception:  # pragma: no cover - celery is an optional dependency
        logger.debug(
            "stapel_core.observability: celery is not importable, the worker "
            "metrics hook is not installed",
            exc_info=True,
        )
        return False

    signals.celeryd_init.connect(_on_worker_start, dispatch_uid=_UID, weak=False)
    signals.beat_init.connect(_on_beat_start, dispatch_uid=_UID, weak=False)
    _installed = True
    return True


def _on_worker_start(sender=None, conf=None, options=None, **kwargs) -> None:
    """``celeryd_init``: the main worker process, before the pool exists."""
    try:
        warning = _prefork_blind_spot(conf, options)
        if warning:
            logger.warning("%s", warning)
    except Exception:  # pragma: no cover - a warning must not stop a worker
        logger.debug(
            "stapel_core.observability: pool inspection failed", exc_info=True
        )
    _serve("celery worker")


def _on_beat_start(sender=None, **kwargs) -> None:
    """``beat_init``: the scheduler process, which serves no HTTP either."""
    _serve("celery beat")


def _serve(what: str) -> None:
    """Open the listener if one was asked for. Never raises."""
    try:
        from .exporter import serve_metrics

        if serve_metrics():
            logger.info(
                "stapel_core.observability: %s metrics listener is up", what
            )
    except Exception:  # pragma: no cover - serve_metrics already guards itself
        logger.error(
            "stapel_core.observability: %s could not start its metrics "
            "listener; it is starting anyway",
            what,
            exc_info=True,
        )


# ─── the prefork blind spot ──────────────────────────────────────────────

_PREFORK_POOLS = frozenset({"prefork", "processes"})

_PREFORK_WARNING = (
    "stapel_core.observability: EXPORTER_PORT is set on a --pool=prefork "
    "worker with concurrency %s and no PROMETHEUS_MULTIPROC_DIR. The metrics "
    "listener runs in this (parent) process, task bodies run in forked "
    "children, and prometheus_client keeps per-process values — so the scrape "
    "will succeed and report NOTHING that a task recorded. Run the worker "
    "with --pool=solo (or --pool=threads), or set PROMETHEUS_MULTIPROC_DIR to "
    "an existing empty writable directory in this container's environment."
)


def _prefork_blind_spot(conf, options) -> str | None:
    """The warning text when this worker's pool cannot report task metrics.

    Returns None when there is nothing to say: multiprocess mode is on, the
    pool is not prefork, or concurrency is explicitly one (a single child
    still writes to its own registry, but ``--concurrency=1`` is a shape an
    operator chose and the fix is the same one line, so it is not worth a
    line in every log).
    """
    from .exporter import multiprocess_dir

    if multiprocess_dir():
        return None

    from .conf import observability_settings

    if observability_settings.EXPORTER_PORT is None:
        return None

    options = options or {}
    pool = options.get("pool") or getattr(conf, "worker_pool", None) or "prefork"
    # A pool may be given as a dotted path to a TaskPool class.
    if str(pool).rsplit(".", 1)[-1].lower() not in _PREFORK_POOLS:
        return None

    concurrency = options.get("concurrency")
    if concurrency in (None, 0):
        concurrency = getattr(conf, "worker_concurrency", None)
    if concurrency in (None, 0):
        # Celery's default: one child per CPU, i.e. more than one anywhere
        # that matters.
        return _PREFORK_WARNING % "<cpu count>"
    if int(concurrency) <= 1:
        return None
    return _PREFORK_WARNING % concurrency
