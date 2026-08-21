"""System checks for cross-service payload validation (tag ``stapel_comm``).

``STAPEL_COMM["VALIDATE_SCHEMAS"]`` is on by default: a Function payload
arriving over HTTP (``comm/http.py``) or NATS comes from another process on
another host, and the registered schema is the only thing between it and the
handler.

Two ways that can be true on paper and false in fact, both of which used to be
silent:

- ``jsonschema`` is not importable, so nothing can be checked. The registry
  now refuses the call, but the first person to learn that should be the
  operator at boot smoke, not a caller mid-request. E-level.
- Validation was turned off deliberately. That is a legitimate choice (a
  closed mesh, a performance floor), but it must not become forgotten
  configuration. W-level.
"""
from __future__ import annotations

import importlib

from django.core import checks

E001_VALIDATOR_MISSING = "stapel_core.comm.E001"
W002_VALIDATION_DISABLED = "stapel_core.comm.W002"
E003_SIGNAL_TRANSPORT_UNRESOLVABLE = "stapel_core.comm.E003"


@checks.register("stapel_comm")
def check_schema_validation(app_configs=None, **kwargs):
    from .config import validation_enabled

    if not validation_enabled():
        return [checks.Warning(
            'STAPEL_COMM["VALIDATE_SCHEMAS"] is off: payloads reaching '
            "@function / @on_action handlers are not checked against their "
            "registered schemas, including payloads arriving from other "
            "services over HTTP or NATS.",
            hint="Remove the setting to validate (the default). Keep it off "
                 "only where every caller is trusted and the cost is "
                 "measured, not assumed.",
            id=W002_VALIDATION_DISABLED,
        )]

    try:
        importlib.import_module("jsonschema")
    except ImportError:
        return [checks.Error(
            'STAPEL_COMM["VALIDATE_SCHEMAS"] is on but jsonschema is not '
            "installed, so no payload can be validated. Any call carrying a "
            "registered schema will be refused at runtime.",
            hint="pip install jsonschema (a stapel-core dependency since "
                 '0.24), or set STAPEL_COMM["VALIDATE_SCHEMAS"] = False to '
                 "state explicitly that this deployment accepts unvalidated "
                 "payloads.",
            id=E001_VALIDATOR_MISSING,
        )]
    return []


@checks.register("stapel_comm")
def check_signal_transport(app_configs=None, **kwargs):
    """A configured Signal transport must resolve at boot.

    ``signal()`` swallows everything downstream of address validation — losing
    a frame is legal by contract, so it must never break a request. That makes
    a typo in ``SIGNAL_TRANSPORT`` the perfect silent failure: the host looks
    configured for realtime and delivers nothing, forever. The operator learns
    it here, not from a user reporting a screen that never updates.

    Not configuring a transport at all is the DEFAULT and never reported: an
    HTTP-only host with signals as no-ops is the supported configuration.
    """
    from .config import comm_setting
    from .signals import _transports

    value = comm_setting("SIGNAL_TRANSPORT", "none")
    if not value or value == "none" or callable(value):
        return []
    if isinstance(value, str) and (value in _transports or "." in value):
        if value in _transports:
            return []
        try:
            from django.utils.module_loading import import_string

            resolved = import_string(value)
        except ImportError as exc:
            return [checks.Error(
                f'STAPEL_COMM["SIGNAL_TRANSPORT"] = {value!r} cannot be '
                f"imported ({exc}). Every signal on this host is dropped "
                f"silently.",
                hint="Point it at a callable transport(stream_key, frame), "
                     'use a registered name (e.g. "channels" from '
                     'stapel-realtime), or set "none" to state that this '
                     "host serves no live observers.",
                id=E003_SIGNAL_TRANSPORT_UNRESOLVABLE,
            )]
        if not callable(resolved):
            return [checks.Error(
                f'STAPEL_COMM["SIGNAL_TRANSPORT"] = {value!r} resolves to '
                f"{type(resolved).__name__}, which is not callable. Every "
                f"signal on this host is dropped silently.",
                hint="The transport contract is transport(stream_key, frame).",
                id=E003_SIGNAL_TRANSPORT_UNRESOLVABLE,
            )]
        return []
    return [checks.Error(
        f'STAPEL_COMM["SIGNAL_TRANSPORT"] = {value!r} is neither "none", a '
        f"registered transport name, nor a dotted path. Every signal on this "
        f"host is dropped silently.",
        hint="Registered names: "
             + (", ".join(sorted(_transports)) or "(none — install and add "
                "the app that registers one, e.g. stapel-realtime)"),
        id=E003_SIGNAL_TRANSPORT_UNRESOLVABLE,
    )]


__all__ = [
    "E001_VALIDATOR_MISSING",
    "W002_VALIDATION_DISABLED",
    "E003_SIGNAL_TRANSPORT_UNRESOLVABLE",
    "check_schema_validation",
    "check_signal_transport",
]
