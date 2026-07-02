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
    # inprocess | http
    "FUNCTION_TRANSPORT": "inprocess",
    # For the http transport: longest-prefix match of function name → base
    # URL of the owning service, e.g. {"cdn.": "http://svc-cdn:8000/cdn"}.
    "FUNCTION_ROUTES": {},
    "FUNCTION_TIMEOUT": 5.0,
    # Validate payloads against schemas registered with @function/@on_action.
    # None = follow settings.DEBUG.
    "VALIDATE_SCHEMAS": None,
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
