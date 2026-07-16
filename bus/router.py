"""
Bus singleton — backend chosen by environment first, Django setting second.

    # environment (12-factor, wins over settings):
    STAPEL_BUS_BACKEND=nats          # shorthand
    STAPEL_BUS_BACKEND=kafka
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

import importlib
import os
import threading

from .base import BusBackend

SHORTHANDS = {
    "memory": "stapel_core.bus.backends.memory.MemoryBus",
    "kafka": "stapel_core.bus.backends.kafka.KafkaBus",
    "nats": "stapel_core.bus.backends.nats.NatsJetStreamBus",
    "routing": "stapel_core.bus.backends.routing.RoutingBus",
}

_bus: BusBackend | None = None
_lock = threading.Lock()


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
    return _bus


def reset_bus() -> None:
    """Force re-initialisation — useful in tests."""
    global _bus
    with _lock:
        _bus = None
