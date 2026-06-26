"""
Bus singleton — returns the backend configured in Django settings.

    STAPEL_BUS_BACKEND = "stapel_core.bus.backends.kafka.KafkaBus"   # prod
    STAPEL_BUS_BACKEND = "stapel_core.bus.backends.memory.MemoryBus"  # tests / dev
"""
from __future__ import annotations

import importlib
import threading

from .base import BusBackend

_bus: BusBackend | None = None
_lock = threading.Lock()


def get_bus() -> BusBackend:
    global _bus
    if _bus is not None:
        return _bus
    with _lock:
        if _bus is not None:
            return _bus
        from django.conf import settings
        dotted = getattr(
            settings,
            "STAPEL_BUS_BACKEND",
            "stapel_core.bus.backends.kafka.KafkaBus",
        )
        module_path, class_name = dotted.rsplit(".", 1)
        module = importlib.import_module(module_path)
        _bus = getattr(module, class_name)()
    return _bus


def reset_bus() -> None:
    """Force re-initialisation — useful in tests."""
    global _bus
    with _lock:
        _bus = None
