"""STAPEL_COMM settings namespace."""
from __future__ import annotations

from typing import Any

_DEFAULTS: dict[str, Any] = {
    # inprocess | bus | memory. "bus" delegates to stapel_core.bus
    # (Kafka/NATS selected by STAPEL_BUS_BACKEND).
    "ACTION_TRANSPORT": "inprocess",
    # Write every emit() through the transactional outbox. Disable only in
    # tests that assert synchronous delivery.
    "OUTBOX_ENABLED": True,
    # emit() with the outbox on but outside transaction.atomic() breaks the
    # "event leaves iff the transaction commits" guarantee (the outbox row
    # commits detached from the mutation). warn (default) | error | allow.
    "EMIT_OUTSIDE_ATOMIC": "warn",
    # inprocess | nats | http | dotted path to a transport callable.
    # nats is the recommended RPC between services: one multiplexed
    # connection per process, protocol-level timeouts, queue-group load
    # balancing, no route table. http remains as a curl-debuggable fallback.
    "FUNCTION_TRANSPORT": "inprocess",
    # For the http transport: longest-prefix match of function name → base
    # URL of the owning service, e.g. {"cdn.": "http://svc-cdn:8000/cdn"}.
    "FUNCTION_ROUTES": {},
    "FUNCTION_TIMEOUT": 5.0,
    # For the nats transport
    "NATS_URL": "nats://nats:4222",
    "NATS_SUBJECT_PREFIX": "stapel.fn",
    # Validate payloads against schemas registered with @function/@on_action.
    # None = follow settings.DEBUG.
    "VALIDATE_SCHEMAS": None,
    # Task execution: inline (in the consumer/relay process) | celery |
    # dotted path to a callable(task_id).
    "TASK_EXECUTOR": "inline",
    # How the ``task.requested`` announcement reaches the worker:
    #   action — ride ACTION_TRANSPORT like any other Action (default);
    #   bus    — publish task.* events directly via stapel_core.bus,
    #            regardless of ACTION_TRANSPORT (monolith keeps Actions
    #            in-process while Tasks go through a broker to a worker);
    #   inline — start() executes the task synchronously (tests/scripts).
    # Orthogonal to TASK_EXECUTOR, which is HOW the worker runs the handler.
    "TASK_DISPATCH": "action",
    # Service name stamped into emitted events; falls back to SERVICE_NAME.
    "SERVICE": None,
}


def comm_setting(name: str, default: Any = None) -> Any:
    from django.conf import settings

    overrides = getattr(settings, "STAPEL_COMM", {}) or {}
    if name in overrides:
        return overrides[name]
    if name in _DEFAULTS:
        value = _DEFAULTS[name]
        return default if value is None and default is not None else value
    return default


def validation_enabled() -> bool:
    from django.conf import settings

    configured = comm_setting("VALIDATE_SCHEMAS")
    if configured is None:
        return bool(getattr(settings, "DEBUG", False))
    return bool(configured)


def service_name() -> str:
    from django.conf import settings

    return (
        comm_setting("SERVICE")
        or getattr(settings, "SERVICE_NAME", "")
        or ""
    ).lower().replace(" ", "-")
