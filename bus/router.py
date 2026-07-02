"""
Bus singleton — backend chosen by environment first, Django setting second.

    # environment (12-factor, wins over settings):
    STAPEL_BUS_BACKEND=nats          # shorthand
    STAPEL_BUS_BACKEND=kafka
    STAPEL_BUS_BACKEND=memory
    STAPEL_BUS_BACKEND=my_app.bus.CustomBus   # or any dotted path

    # Django settings (fallback, same forms):
    STAPEL_BUS_BACKEND = "memory"

Default is kafka for backward compatibility; new deployments should set
``nats`` (JetStream) — see docs/module-communication.md.
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
        dotted = "kafka"
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
