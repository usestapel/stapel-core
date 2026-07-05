"""comm surface for control-plane callers.

Registered from the Django app's ``ready()``:

- ``gateway.invoke`` — invoke a verb on behalf of a project from inside
  the control plane (channel ``comm``; no scope token — comm transport
  security is the service-to-service layer's business, e.g. the function
  HTTP transport's ``X-API-KEY``);
- ``gateway.confirm`` — resolve a parked two-phase call. Lives *only*
  here and in the Python API — never on the container HTTP surface.
"""
from __future__ import annotations

from typing import Any

from . import service
from .base import CallerContext, PendingConfirmation

INVOKE_SCHEMA = {
    "type": "object",
    "properties": {
        "verb": {"type": "string", "minLength": 1},
        "args": {"type": "object"},
        "project": {"type": ["string", "null"]},
        "container": {"type": ["string", "null"]},
        "tier": {"type": ["string", "null"]},
        "subject": {"type": ["string", "null"]},
    },
    "required": ["verb"],
    "additionalProperties": False,
}

CONFIRM_SCHEMA = {
    "type": "object",
    "properties": {
        "confirmation_id": {"type": "string", "minLength": 1},
        "approved_by": {"type": "string", "minLength": 1},
        "approve": {"type": "boolean"},
    },
    "required": ["confirmation_id", "approved_by"],
    "additionalProperties": False,
}


def invoke_function(payload: dict) -> dict[str, Any]:
    caller = CallerContext(
        channel="comm",
        project=payload.get("project"),
        container=payload.get("container"),
        tier=payload.get("tier"),
        subject=payload.get("subject"),
    )
    result = service.invoke(payload["verb"], payload.get("args") or {}, caller=caller)
    if isinstance(result, PendingConfirmation):
        return {
            "status": "pending",
            "confirmation_id": result.confirmation_id,
            "expires_at": result.expires_at.isoformat(),
        }
    return {"status": "ok", "result": result}


def confirm_function(payload: dict) -> dict[str, Any]:
    result = service.confirm(
        payload["confirmation_id"],
        approved_by=payload["approved_by"],
        approve=payload.get("approve", True),
    )
    return {"status": "ok", "result": result}


def register() -> None:
    from stapel_core.comm import register_function

    register_function("gateway.invoke", invoke_function, schema=INVOKE_SCHEMA)
    register_function("gateway.confirm", confirm_function, schema=CONFIRM_SCHEMA)


__all__ = ["confirm_function", "invoke_function", "register"]
