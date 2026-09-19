"""
Bus singleton — backend chosen by environment first, Django setting second.

    # environment (12-factor, wins over settings):
    STAPEL_BUS_BACKEND=nats          # shorthand
    STAPEL_BUS_BACKEND=kafka
    STAPEL_BUS_BACKEND=redis_streams # or the alias: redis
    STAPEL_BUS_BACKEND=memory
    STAPEL_BUS_BACKEND=routing       # per-topic-prefix routes (STAPEL_BUS_ROUTES)
    STAPEL_BUS_BACKEND=my_app.bus.CustomBus   # or any dotted path

    # Django settings (fallback, same forms):
    STAPEL_BUS_BACKEND = "memory"

Default is ``memory`` — synchronous in-process delivery to subscribers of
*this* process, the correct semantics for a dev box or a monolith with no
broker. Kafka/NATS are explicit opt-in via ``STAPEL_BUS_BACKEND`` (env or
setting); a deployment that needs cross-process delivery must configure one
of them explicitly — see docs/module-communication.md. (Before 0.11.0 the
default was ``kafka``: a deployment that never installed ``confluent-kafka``
and never set ``STAPEL_BUS_BACKEND`` got a ``ModuleNotFoundError`` on every
publish, silently swallowed by callers that fail-soft on publish errors —
see ``stapel_core.bus.checks`` for the system check that now catches this at
boot instead.)
"""
from __future__ import annotations

import atexit
import importlib
import logging
import os
import threading

from .base import DEFAULT_FLUSH_TIMEOUT, BusBackend

logger = logging.getLogger(__name__)

SHORTHANDS = {
    "memory": "stapel_core.bus.backends.memory.MemoryBus",
    "kafka": "stapel_core.bus.backends.kafka.KafkaBus",
    "nats": "stapel_core.bus.backends.nats.NatsJetStreamBus",
    "redis_streams": "stapel_core.bus.backends.redis_streams.RedisStreamsBus",
    "redis": "stapel_core.bus.backends.redis_streams.RedisStreamsBus",  # alias
    "routing": "stapel_core.bus.backends.routing.RoutingBus",
}

_bus: BusBackend | None = None
_lock = threading.Lock()
_atexit_registered = False


def _resolve_backend_path() -> str:
    dotted = os.environ.get("STAPEL_BUS_BACKEND", "")
    if not dotted:
        try:
            from django.conf import settings

            dotted = getattr(settings, "STAPEL_BUS_BACKEND", "") or ""
        except Exception:  # settings not configured
            dotted = ""
    if not dotted:
        dotted = "memory"
    return SHORTHANDS.get(dotted, dotted)


def get_bus() -> BusBackend:
    global _bus
    if _bus is not None:
        return _bus
    with _lock:
        if _bus is not None:
            return _bus
        dotted = _resolve_backend_path()
        module_path, class_name = dotted.rsplit(".", 1)
        module = importlib.import_module(module_path)
        _bus = getattr(module, class_name)()
    _register_atexit_flush()
    return _bus


def reset_bus() -> None:
    """Force re-initialisation — useful in tests."""
    global _bus
    with _lock:
        _bus = None


# ----------------------------------------------------------------------
# Flush
# ----------------------------------------------------------------------


def flush_timeout() -> float:
    """``STAPEL_BUS_FLUSH_TIMEOUT`` (env, then setting), default 5 seconds.

    Bounded on purpose: the wait is on the exit path of a process that has
    finished its work, and an unreachable broker must delay that exit by a
    known number of seconds, not forever.
    """
    from ._config import _get

    raw = _get("STAPEL_BUS_FLUSH_TIMEOUT", "")
    if not raw:
        return DEFAULT_FLUSH_TIMEOUT
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "STAPEL_BUS_FLUSH_TIMEOUT=%r is not a number — using %ss",
            raw, DEFAULT_FLUSH_TIMEOUT,
        )
        return DEFAULT_FLUSH_TIMEOUT


def flush(timeout: float | None = None) -> int:
    """Wait for everything this process published to reach the broker.

    Returns the number of messages still undelivered when the wait ended; 0
    is the good answer and the only one that means "nothing was lost".

    Deliberately does NOT create a backend: a process that never published
    has nothing in flight, and dialling a broker to discover that — on the
    way out, no less — is a connection nobody asked for.
    """
    bus = _bus
    if bus is None:
        return 0
    remaining = bus.flush(flush_timeout() if timeout is None else timeout)
    if remaining:
        logger.warning(
            "bus flush: %s message(s) still undelivered after the timeout — "
            "they are lost if this process exits now. Raise "
            "STAPEL_BUS_FLUSH_TIMEOUT, or check the broker.",
            remaining,
        )
    return remaining


def _flush_at_exit() -> None:
    """atexit hook — the backstop for every short-lived process.

    WHY atexit and not a wrapper around ``BaseCommand.execute``: the losing
    process is not always a management command. It is a command, yes, but
    also a celery task process, a script that calls ``django.setup()``, a
    one-shot container entrypoint and a gunicorn worker being recycled —
    and a celery worker in particular NEVER goes through
    ``BaseCommand.execute`` for the code that publishes, because the task
    runs in a pool process long after the ``celery worker`` command started.
    A command wrapper would cover one of those five and would have to
    monkey-patch a Django class from ``ready()`` to do it. ``atexit`` covers
    all five, from the place that knows a producer now exists, with no hook
    into anyone else's lifecycle. Its one blind spot — ``os._exit``,
    ``SIGKILL``, a segfault — is a blind spot the command wrapper shares.

    Never raises: this runs while the interpreter is shutting down, and a
    broker that has gone away must not turn a finished job into a traceback
    and a non-zero exit status.
    """
    try:
        flush()
    except Exception:  # noqa: BLE001 — exit path, never fatal
        logger.warning("bus flush at exit failed", exc_info=True)


def _register_atexit_flush() -> None:
    global _atexit_registered
    if _atexit_registered:
        return
    with _lock:
        if _atexit_registered:
            return
        atexit.register(_flush_at_exit)
        _atexit_registered = True


def _on_settings_changed(*, setting, **kwargs) -> None:
    """Auto-invalidate the cached backend when ``STAPEL_BUS_BACKEND`` changes.

    ``get_bus()`` reads the backend lazily (only on the first real call, not
    at import time) — but once resolved it is a module-level singleton for
    the rest of the process. If something calls ``get_bus()`` once before a
    later ``override_settings(STAPEL_BUS_BACKEND=...)`` (or the
    pytest-django ``settings`` fixture, which this whole test suite uses),
    the *first* resolved backend stuck around regardless — exactly the shape
    of bug the owner caught live (a process that resolved kafka once stays
    on kafka until a restart, however the setting changes afterward).
    Connected to Django's ``setting_changed`` signal (the same mechanism DRF
    uses to invalidate its own cached ``api_settings``), so any runtime
    reconfiguration path — tests first and foremost, but also a management
    shell or a future dynamic-settings admin toggle — invalidates the
    singleton automatically instead of silently keeping the stale backend.

    Production's env-based config needs no such hook: ``os.environ`` is read
    once at boot and does not change without a process restart, so there is
    nothing to invalidate there — the very first ``get_bus()`` call already
    sees the final value.
    """
    if setting == "STAPEL_BUS_BACKEND":
        reset_bus()


try:
    from django.test.signals import setting_changed

    setting_changed.connect(_on_settings_changed)
except Exception:  # pragma: no cover — django.test is always importable in practice
    pass
