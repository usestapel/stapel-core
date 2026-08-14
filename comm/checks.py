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


__all__ = [
    "E001_VALIDATOR_MISSING",
    "W002_VALIDATION_DISABLED",
    "check_schema_validation",
]
