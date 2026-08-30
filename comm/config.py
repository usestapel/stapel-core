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
    # On by default: a Function payload arriving over HTTP or NATS comes from
    # another service across the network, and the schema is the only thing
    # standing between it and the handler. This used to follow settings.DEBUG,
    # which meant validation was on where payloads are hand-written and off in
    # production — dev and prod ran different code paths, and the one that
    # mattered was the unchecked one. Set False to opt out explicitly.
    "VALIDATE_SCHEMAS": True,
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
    # Signal delivery backend: "none" (default — signal() is a silent no-op,
    # the correct configuration for every HTTP-only host), a name registered
    # via comm.register_signal_transport() ("channels", registered by
    # stapel-realtime), or a dotted path to transport(stream_key, frame).
    # Closed by default on purpose: emitting must cost nothing for the 26
    # libraries that never serve a WebSocket. The future "bus" value carries
    # signals over NATS stapel.ws.* in microservice mode — same seam.
    "SIGNAL_TRANSPORT": "none",
    # What to do when an action handler lets a django ValidationError escape:
    # the payload decoded, reached working code, and that code refused its
    # values (a malformed id an ORM field cannot coerce). Redelivering it
    # produces the same refusal forever — a poison pill that blocks the
    # partition behind it. park (default) counts it in the DLQ metric and
    # acks; raise restores pre-0.53 behaviour for a deployment that would
    # rather stop the line. See comm/actions.py:deliver_to_subscribers.
    "UNPROCESSABLE_PAYLOAD": "park",
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
    """Whether registered schemas are enforced.

    Deliberately independent of ``settings.DEBUG``: tying a security control to
    the debug flag makes production the only environment that never runs it.
    """
    return bool(comm_setting("VALIDATE_SCHEMAS"))


def service_name() -> str:
    from django.conf import settings

    return (
        comm_setting("SERVICE")
        or getattr(settings, "SERVICE_NAME", "")
        or ""
    ).lower().replace(" ", "-")
