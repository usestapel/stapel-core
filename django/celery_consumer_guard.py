"""A worker that does not consume its own default queue must not start.

The sibling check ``django/celery_checks.py`` closes the half of this class a
Django system check can see: the default queue naming a *different app*. This
module closes the half it cannot see by construction. ``manage.py check`` runs
in a process that has no worker in it and no command line to read, so nothing
in the framework had ever compared the queues a worker actually consumes with
the queue its own beat publishes to.

Measured on a client stand, 2026-09-09. One service's worker was started as::

    celery -A core worker -Q cdn,thumbnails,previews,celery

while its settings said ``CELERY_TASK_DEFAULT_QUEUE = "stapel_cdn"``. ``-Q``
**replaces** the consumed set, it does not extend it — ``WorkController.
setup_queues()`` calls ``app.amqp.queues.select(include)``, and ``select()``
overwrites ``_consume_from`` outright. So the four named queues were consumed
and ``stapel_cdn`` was consumed by nobody:

- every beat entry and every task with no ``task_routes`` match published
  there — including this library's own
  ``stapel_core.django.taskstore.sweep_tasks``;
- **27,234 messages** accumulated over months, unconsumed;
- and because the sweep is what wakes a retry held on ``not_before``
  (``taskstore/beat.py``), every failed task in that service was "retrying"
  forever and never retried.

Nothing was red. The worker reported ready, its four queues drained normally,
the broker's depth on a fifth queue was the only witness, and renaming the
default queue merely moved the leak to the new name.

**Fail-closed, same reasoning as E001.** A worker that leaves its own queue
unconsumed is a correct-looking process from every angle a monitor has — the
container is up, tasks complete, the error rate is zero. A warning would join
the log noise it exists to interrupt, which is exactly how this survived. So
the guard raises and the worker refuses to boot.

**Raising takes care.** Celery's ``Signal.send`` catches ``Exception`` from
every receiver, logs it and carries on (``celery/utils/dispatch/signal.py``);
a plain ``raise RuntimeError`` here would be *logged and ignored*, which is
this defect wearing a guard. :class:`PartialConsumerError` derives from
``SystemExit`` — a ``BaseException``, so it escapes that ``except`` clause,
escapes ``Worker.on_start`` and lands in ``WorkController.start``'s
``except SystemExit: self.stop(exitcode=exc.code)``. The worker stops before
the consumer is ever created, and the message is the exit code, so it reaches
stderr even where logging is not configured.

**The signal.** ``celeryd_after_setup``, not ``worker_ready``, and not
``celeryd_init``. ``setup_queues()`` runs from ``WorkController.__init__``,
``celeryd_init`` is sent *inside* that constructor before it, and
``celeryd_after_setup`` is sent from ``Worker.on_start`` — Celery's own
comment there reads "this signal can be used to, for example, change queues
after the ``-Q`` option has been applied". It is the earliest point at which
``app.amqp.queues.consume_from`` is the real consumed set, and it is before
the worker has connected to a broker or claimed a message. ``worker_ready``
would also see it, but only after the process has begun consuming.

**The one escape.** ``STAPEL_CELERY_ALLOW_PARTIAL_CONSUMER = True`` (flat
setting, the ``STAPEL_BLACKLIST_FAIL_OPEN`` idiom — a single boolean that
turns one gate down, not a namespace) downgrades the refusal to a warning,
for a deployment that deliberately runs several workers over disjoint queues.
Such a deployment still has to make sure **some** worker consumes the default
queue: this guard sees one process and can say nothing about the fleet around
it.

**Routed queues get a warning, never a refusal.** ``task_routes`` entries
naming a queue this worker does not consume are the same defect one level
down, and ``conf.task_routes`` is right there, so each one is named at
startup. Warning only, because a split-worker deployment routing a queue to
its own worker is the normal, correct shape — and unlike the default queue,
whose owner is unambiguous, this process genuinely cannot tell.

Installed by ``stapel_core.django``'s ``AppConfig.ready()`` next to the
observability hook, so no service opts in. A project that does not install
the ``stapel_core.django`` app puts one line in ``config/celery.py``::

    from stapel_core.django.celery_consumer_guard import install
    install(force=True)
"""
from __future__ import annotations

import logging
import sys
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "ALLOW_PARTIAL_SETTING",
    "PartialConsumerError",
    "consumed_queues",
    "default_queue_allowed",
    "default_queue_refusal",
    "install",
    "is_installed",
    "routed_queue_warning",
    "routed_queues",
    "verify_consumer_queues",
]

#: Flat Django setting that downgrades the refusal to a warning.
ALLOW_PARTIAL_SETTING = "STAPEL_CELERY_ALLOW_PARTIAL_CONSUMER"

#: One uid so a second ready() replaces rather than duplicates the receiver.
_UID = "stapel_core.django.celery_consumer_guard"

_installed = False


class PartialConsumerError(SystemExit):
    """Refusal to start a worker that does not consume its default queue.

    ``SystemExit`` deliberately: see the module docstring — Celery's signal
    dispatcher swallows every ``Exception`` a receiver raises.
    """

    def __init__(self, message: str) -> None:
        # .code is the message, so sys.exit() prints it and exits 1 even in a
        # process whose logging was never configured.
        super().__init__(message)
        self.message = message


def is_installed() -> bool:
    """Whether the startup handler is connected in this process."""
    return _installed


def install(force: bool = False) -> bool:
    """Connect the guard to Celery's ``celeryd_after_setup``.

    Idempotent. ``force=False`` (how ``AppConfig.ready()`` calls it) connects
    only when ``celery`` is already imported — under ``celery -A config
    worker`` the Celery CLI is the entry point, so the package is in
    ``sys.modules`` long before Django is set up, and a plain web or
    ``manage.py`` process does not pay for an import it will never use.

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
            "stapel_core: celery is not importable, the consumer guard is "
            "not installed",
            exc_info=True,
        )
        return False

    signals.celeryd_after_setup.connect(
        _on_worker_setup, dispatch_uid=_UID, weak=False
    )
    _installed = True
    return True


def _on_worker_setup(sender=None, instance=None, conf=None, **kwargs) -> None:
    """``celeryd_after_setup``: queues are selected, nothing is consumed yet."""
    try:
        app = getattr(instance, "app", None)
        if app is None:  # pragma: no cover - the signal carries the worker
            from celery import current_app

            app = current_app
        if conf is None:  # pragma: no cover - likewise
            conf = app.conf

        consumed = consumed_queues(app)
        default = getattr(conf, "task_default_queue", None)
        routed = routed_queues(getattr(conf, "task_routes", None))
    except Exception:
        # Reading the worker's own configuration must never be what stops it.
        logger.exception(
            "stapel_core: could not read this worker's queue set; the "
            "default-queue guard did not run"
        )
        return

    verify_consumer_queues(
        default_queue=default,
        consumed=consumed,
        routed=routed,
        allow_partial=_allow_partial(),
        hostname=sender,
    )


def _allow_partial() -> bool:
    """Read the escape hatch, tolerating a process with no Django settings."""
    try:
        from django.conf import settings

        return bool(getattr(settings, ALLOW_PARTIAL_SETTING, False))
    except Exception:  # pragma: no cover - a worker outside Django
        return False


# ─── reading the worker ──────────────────────────────────────────────────


def consumed_queues(app) -> list[str]:
    """The queue names this worker will consume, in a stable order.

    ``app.amqp.queues.consume_from`` is the selection ``-Q`` produced; with
    no ``-Q`` it is every declared queue, which includes the default.
    """
    return sorted(app.amqp.queues.consume_from)


def routed_queues(task_routes) -> dict[str, list[str]]:
    """``{queue name: [routing patterns that send work there]}``.

    Only the declarative dict/sequence forms are read. ``task_routes`` may
    also hold a callable or a dotted path to a router, and asking a router
    where a task goes means calling host code at worker startup — this guard
    does not. Such a deployment simply gets no routed-queue warnings.
    """
    routes: dict[str, list[str]] = {}
    for pattern, route in _iter_routes(task_routes):
        queue = route.get("queue") if isinstance(route, dict) else None
        if isinstance(queue, str) and queue:
            routes.setdefault(queue, []).append(str(pattern))
    return routes


def _iter_routes(task_routes) -> Iterable[tuple[object, object]]:
    if isinstance(task_routes, dict):
        return list(task_routes.items())
    if isinstance(task_routes, (list, tuple)):
        pairs: list[tuple[object, object]] = []
        for entry in task_routes:
            if isinstance(entry, dict):
                pairs.extend(entry.items())
            elif isinstance(entry, (list, tuple)) and len(entry) == 2:
                pairs.append((entry[0], entry[1]))
        return pairs
    return ()


# ─── the messages ────────────────────────────────────────────────────────


def _quote(names: Sequence[str]) -> str:
    return ", ".join(f'"{name}"' for name in names) or "(nothing)"


def default_queue_refusal(default_queue: str, consumed: Sequence[str]) -> str:
    """Text of the refusal — names the queue, the set, and both repairs."""
    return (
        f'stapel_core: this Celery worker does not consume its own default '
        f'queue "{default_queue}". It consumes {_quote(consumed)}. '
        "-Q REPLACES the consumed set, it does not extend it, so every task "
        "published without an explicit route — every beat entry, every task "
        "with no task_routes match, including this library's own "
        "stapel_core.django.taskstore.sweep_tasks — piles up on "
        f'"{default_queue}" unread, and no retry held on not_before is ever '
        "woken. On a client stand that was 27,234 messages over months with "
        "nothing red anywhere. Two ways to fix it: drop -Q, so the worker "
        f'consumes task_default_queue; or name "{default_queue}" in it '
        f'(-Q {",".join([default_queue, *consumed])}). A deployment that '
        "deliberately splits work across workers on disjoint queues sets "
        f"{ALLOW_PARTIAL_SETTING} = True — and must then ensure SOME worker "
        f'consumes "{default_queue}"; this guard sees only its own process. '
        "Refusing to start on purpose: a worker that leaves its own queue "
        "unconsumed looks healthy from every angle a monitor has."
    )


def default_queue_allowed(default_queue: str, consumed: Sequence[str]) -> str:
    """Text of the downgraded warning under the escape hatch."""
    return (
        f'stapel_core: this Celery worker does not consume its own default '
        f'queue "{default_queue}" (it consumes {_quote(consumed)}), and '
        f"{ALLOW_PARTIAL_SETTING} is on, so it is starting anyway. Some "
        f'worker in this deployment must consume "{default_queue}" or every '
        "unrouted task and every beat entry piles up there unread; this "
        "guard checks only the process it runs in."
    )


def routed_queue_warning(
    queue: str, patterns: Sequence[str], consumed: Sequence[str]
) -> str:
    """Text of the per-routed-queue warning."""
    return (
        f'stapel_core: task_routes sends {_quote(patterns)} to queue '
        f'"{queue}", which this worker does not consume (it consumes '
        f"{_quote(consumed)}). Warning, not a refusal: a fleet may well run "
        "another worker for that queue, and this guard sees one process. If "
        "none does, those tasks accumulate unread exactly as an unconsumed "
        "default queue does."
    )


# ─── the guard itself ────────────────────────────────────────────────────


def verify_consumer_queues(
    *,
    default_queue: str | None,
    consumed: Sequence[str],
    routed: dict[str, list[str]] | None = None,
    allow_partial: bool = False,
    hostname: str | None = None,
) -> list[str]:
    """Refuse (or warn) when this worker's consumed set is incomplete.

    Raises :class:`PartialConsumerError` when *default_queue* is not in
    *consumed* and *allow_partial* is false. Returns the warnings it logged,
    so a caller (and a test) can read them without a log handler.
    """
    consumed = list(consumed)
    warnings: list[str] = []
    where = f" [{hostname}]" if hostname else ""

    if default_queue and default_queue not in consumed:
        if not allow_partial:
            message = default_queue_refusal(default_queue, consumed)
            logger.critical("%s%s", message, where)
            raise PartialConsumerError(message)
        message = default_queue_allowed(default_queue, consumed)
        logger.warning("%s%s", message, where)
        warnings.append(message)

    for queue in sorted(routed or {}):
        if queue in consumed or queue == default_queue:
            continue
        message = routed_queue_warning(queue, (routed or {})[queue], consumed)
        logger.warning("%s%s", message, where)
        warnings.append(message)

    return warnings
