"""Audit trail (S6 — no holes).

Every invocation attempt produces exactly one audit line — allowed and
executed, denied by any check, parked pending confirmation, confirmed,
rejected, expired. The line answers *who / what / when / through which
door / with what outcome*.

The sink is a dotted-path seam (``STAPEL_GATEWAY["AUDIT_SINK"]``);
the default appends to :mod:`stapel_core.eventstore` (stream ``audit`` or
the verb's ``policy.audit_stream``). Studio swaps the sink or routes the
stream without touching this module.

Fail-closed, fail-noisy: a sink exception is wrapped in
:class:`AuditFailure` and propagates — a denial is still denied, and an
executed call reports failure to its caller even though the handler ran.
Note the default sink buffers in-process (eventstore WriteBuffer); a
deployment that wants a synchronous durability guarantee per line sets
``STAPEL_EVENTSTORE["BUFFER_SYNC"]`` or plugs a synchronous sink.

Args are recorded verbatim up to ``AUDIT_ARGS_MAXLEN`` characters of
canonical JSON; larger payloads are replaced by a sha256 fingerprint so
the line stays bounded but remains matchable.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .base import CallerContext
from .conf import gateway_settings
from .exceptions import AuditFailure


def eventstore_sink(stream: str, payload: dict, *, project: str | None, container: str | None) -> None:
    """Default sink: append to the core event store."""
    from stapel_core import eventstore

    eventstore.append(stream, payload, project=project, container=container)


def _args_for_audit(args: dict) -> dict[str, Any]:
    try:
        canonical = json.dumps(args, sort_keys=True, default=repr)
    except Exception:  # pragma: no cover - json.dumps with default=repr is total
        canonical = repr(args)
    maxlen = int(gateway_settings.AUDIT_ARGS_MAXLEN)
    if len(canonical) <= maxlen:
        return {"args": args}
    return {
        "args_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "args_size": len(canonical),
    }


def record(
    *,
    verb: str | None,
    decision: str,
    caller: CallerContext,
    args: dict | None = None,
    reason: str | None = None,
    ok: bool | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
    confirmation_id: str | None = None,
    audit_stream: str | None = None,
) -> None:
    """Write one audit line. ``decision`` ∈ ``executed | denied | pending |
    rejected | expired``. Raises :class:`AuditFailure` if the sink fails."""
    payload: dict[str, Any] = {
        "verb": verb,
        "decision": decision,
        "channel": caller.channel,
        "tier": caller.tier,
        "ip": caller.ip,
        "token_id": caller.token_id,
        "subject": caller.subject,
    }
    if args is not None:
        payload.update(_args_for_audit(args))
    if reason is not None:
        payload["reason"] = reason
    if ok is not None:
        payload["ok"] = ok
    if error is not None:
        payload["error"] = error
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    if confirmation_id is not None:
        payload["confirmation_id"] = confirmation_id
    if caller.confirmed_by is not None:
        payload["confirmed_by"] = caller.confirmed_by

    stream = audit_stream or gateway_settings.AUDIT_STREAM
    sink = gateway_settings.AUDIT_SINK
    try:
        sink(stream, payload, project=caller.project, container=caller.container)
    except Exception as exc:
        raise AuditFailure(
            f"audit sink failed for verb {verb!r} (decision {decision!r}): {exc!r}"
        ) from exc


__all__ = ["eventstore_sink", "record"]
