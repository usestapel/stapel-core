"""System check for the message-bus backend (tag ``stapel_bus``).

E-level — the effective ``STAPEL_BUS_BACKEND`` names a known transport whose
client library is not importable in this environment. This is meant to be
caught at ``manage.py check`` / boot-smoke time, not on the first
``publish()`` call in production: a Kafka/NATS backend without its extra
installed raises ``ModuleNotFoundError`` deep inside the backend, which a
fail-soft caller (``notifications.request_notification``, by contract) turns
into a silently dropped event — e.g. OTP emails that never left the process,
with only a logged traceback nobody was watching.
"""
from __future__ import annotations

import importlib

from django.core import checks

E001_MISSING_TRANSPORT_LIBRARY = "stapel_core.bus.E001"

#: Backend shorthand -> (module to probe for importability, pip extra name).
#: ``memory`` and ``routing`` need no extra third-party client and are
#: omitted; ``routing`` fans out to other backends, each checked on their own
#: merits if also named directly.
_TRANSPORT_LIBRARIES = {
    "kafka": ("confluent_kafka", "kafka"),
    "nats": ("nats", "nats"),
    "redis_streams": ("redis", "redis-bus"),
}


def _configured_shorthand() -> str | None:
    """The shorthand name of the effective backend, or None (dotted/custom)."""
    from .router import SHORTHANDS, _resolve_backend_path

    dotted = _resolve_backend_path()
    for shorthand, path in SHORTHANDS.items():
        if path == dotted:
            return shorthand
    return None


@checks.register("stapel_bus")
def check_bus_backend_library(app_configs=None, **kwargs):
    """E001 — the configured bus backend's transport library is missing."""
    shorthand = _configured_shorthand()
    if shorthand is None:
        return []
    probe = _TRANSPORT_LIBRARIES.get(shorthand)
    if probe is None:
        return []
    module_name, extra = probe
    try:
        importlib.import_module(module_name)
    except ImportError:
        return [
            checks.Error(
                f"bus backend {shorthand!r} сконфигурирован (STAPEL_BUS_BACKEND), "
                f"но {module_name!r} не установлен — publish()/consume() будут "
                "падать в рантайме (ModuleNotFoundError на каждый вызов).",
                hint=f"pip install 'stapel-core[{extra}]'",
                id=E001_MISSING_TRANSPORT_LIBRARY,
            )
        ]
    return []


__all__ = [
    "E001_MISSING_TRANSPORT_LIBRARY",
    "check_bus_backend_library",
]
