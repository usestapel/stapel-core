"""Invocation pipeline: resolve → validate → policy → (confirm?) → execute.

Every exit of :func:`invoke` — including every refusal — emits exactly one
audit line (S6). The pipeline is strict about untrusted input (S5): the
verb's JSON schema is validated *before* policy and handler see the args,
and schema validation is mandatory — if the validator is unavailable the
call fails closed, never open.

Two-phase confirmation (contract decision): a verb whose policy sets
``require_confirmation`` does not execute on invoke. The validated call is
parked as a ``PendingAction`` row and ``invoke`` returns a
:class:`PendingConfirmation`. Executing it takes a separate
:func:`confirm` — which is deliberately **not** on the container HTTP
surface: the confirming identity (a human in Studio's UI, a control-plane
service) travels through the comm Function / Python API. On approval the
call re-passes schema + policy (state may have changed since parking) and
runs with ``confirmed_by`` stamped into context and audit.
"""
from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

from django.utils import timezone

from . import audit
from .base import CallerContext, PendingConfirmation, VerbDeclaration
from .conf import gateway_settings
from .exceptions import (
    ArgsInvalid,
    ConfirmationInvalid,
    GatewayConfigError,
    GatewayError,
    HandlerError,
)
from .policy import get_policy_engine
from .registry import verb_registry


def _validate_args(declaration: VerbDeclaration, args: dict) -> None:
    """S5: nothing unvalidated reaches a handler. Fail-closed on a missing
    validator — a privilege gateway must not degrade to best-effort."""
    try:
        import jsonschema
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise GatewayConfigError(
            "jsonschema is required to validate verb arguments "
            "(pip install stapel-core[gateway])"
        ) from exc
    try:
        jsonschema.validate(args, declaration.schema)
    except jsonschema.ValidationError as exc:
        raise ArgsInvalid(
            f"arguments for verb {declaration.name!r} violate its schema: {exc.message}"
        ) from exc


def _resolve_handler(declaration: VerbDeclaration):
    handler = declaration.handler
    if isinstance(handler, str):
        from django.utils.module_loading import import_string

        try:
            handler = import_string(handler)
        except ImportError as exc:
            raise GatewayConfigError(
                f"handler {declaration.handler!r} of verb {declaration.name!r} "
                f"cannot be imported: {exc}"
            ) from exc
    if not callable(handler):
        raise GatewayConfigError(
            f"handler of verb {declaration.name!r} is not callable"
        )
    return handler


def invoke(
    name: str,
    args: dict | None = None,
    *,
    caller: CallerContext,
    _pending=None,
) -> Any:
    """Invoke verb *name* for *caller*. Returns the handler's result, or a
    :class:`PendingConfirmation` when policy parks the call.

    ``_pending`` is the internal re-entry point used by :func:`confirm`;
    it marks the call as already confirmed.
    """
    args = dict(args or {})

    # 1. Deny-by-default: an undeclared verb does not exist.
    try:
        declaration = verb_registry.resolve(name)
    except GatewayError as exc:
        audit.record(verb=name, decision="denied", caller=caller, args=args, reason=exc.reason)
        raise

    stream = declaration.policy.audit_stream

    def _deny(exc: GatewayError, **extra) -> None:
        audit.record(
            verb=name, decision="denied", caller=caller, args=args,
            reason=exc.reason, audit_stream=stream, **extra,
        )

    # 2. Schema validation of untrusted input (S5).
    try:
        _validate_args(declaration, args)
    except GatewayError as exc:
        _deny(exc)
        raise

    # 3. Policy (tiers, rate limit, deployment checks).
    try:
        get_policy_engine().check(declaration, args, caller)
    except GatewayError as exc:
        _deny(exc)
        raise

    # 4. Two-phase confirmation: park instead of executing.
    if declaration.policy.require_confirmation and _pending is None:
        from stapel_core.django.gateway.models import PendingAction

        pending = PendingAction.objects.create(
            verb=name,
            args=args,
            channel=caller.channel,
            project=caller.project,
            container=caller.container,
            tier=caller.tier,
            subject=caller.subject,
            expires_at=timezone.now()
            + timedelta(seconds=int(gateway_settings.CONFIRMATION_TTL)),
        )
        audit.record(
            verb=name, decision="pending", caller=caller, args=args,
            confirmation_id=str(pending.id), audit_stream=stream,
        )
        return PendingConfirmation(
            confirmation_id=str(pending.id),
            verb=name,
            expires_at=pending.expires_at,
        )

    # 5. Execute. Success and handler failure both land on the audit
    #    stream; an audit-sink failure surfaces as AuditFailure (S6:
    #    fail-noisy, never a silent unaudited privileged action).
    try:
        handler = _resolve_handler(declaration)
    except GatewayError as exc:
        _deny(exc)
        raise

    confirmation_id = str(_pending.id) if _pending is not None else None
    started = time.monotonic()
    try:
        result = handler(args, caller)
    except Exception as exc:
        audit.record(
            verb=name, decision="executed", caller=caller, args=args,
            ok=False, error=repr(exc),
            duration_ms=int((time.monotonic() - started) * 1000),
            confirmation_id=confirmation_id, audit_stream=stream,
        )
        raise HandlerError(f"verb {name!r} handler failed: {exc!r}") from exc
    audit.record(
        verb=name, decision="executed", caller=caller, args=args,
        ok=True, duration_ms=int((time.monotonic() - started) * 1000),
        confirmation_id=confirmation_id, audit_stream=stream,
    )
    return result


def confirm(confirmation_id: str, *, approved_by: str, approve: bool = True) -> Any:
    """Resolve a parked call: execute it (``approve=True``) or reject it.

    Control-plane only — not reachable with a container scope token. The
    approval leg re-runs schema + policy before executing.
    """
    from stapel_core.django.gateway.models import PendingAction

    if not approved_by:
        raise ValueError("confirm() requires the confirming identity (approved_by)")

    row = PendingAction.objects.filter(pk=confirmation_id).first()
    audit_caller = CallerContext(channel="internal", subject=approved_by)
    if row is None:
        audit.record(
            verb=None, decision="denied", caller=audit_caller,
            reason="confirmation_unknown", confirmation_id=str(confirmation_id),
        )
        raise ConfirmationInvalid(f"unknown confirmation {confirmation_id!r}",
                                  reason="confirmation_unknown")

    caller = CallerContext(
        channel=row.channel,
        project=row.project,
        container=row.container,
        tier=row.tier,
        subject=row.subject,
        confirmed_by=approved_by,
    )
    now = timezone.now()

    if row.status != PendingAction.STATUS_PENDING:
        audit.record(
            verb=row.verb, decision="denied", caller=caller,
            reason="confirmation_resolved", confirmation_id=str(row.id),
        )
        raise ConfirmationInvalid(
            f"confirmation {row.id} is already {row.status}",
            reason="confirmation_resolved",
        )

    if row.expires_at <= now:
        PendingAction.objects.filter(
            pk=row.pk, status=PendingAction.STATUS_PENDING
        ).update(status=PendingAction.STATUS_EXPIRED, resolved_at=now, resolved_by=approved_by)
        audit.record(
            verb=row.verb, decision="expired", caller=caller,
            reason="confirmation_expired", confirmation_id=str(row.id),
        )
        raise ConfirmationInvalid(
            f"confirmation {row.id} expired", reason="confirmation_expired"
        )

    if not approve:
        claimed = PendingAction.objects.filter(
            pk=row.pk, status=PendingAction.STATUS_PENDING
        ).update(status=PendingAction.STATUS_REJECTED, resolved_at=now, resolved_by=approved_by)
        if not claimed:
            raise ConfirmationInvalid(
                f"confirmation {row.id} was resolved concurrently",
                reason="confirmation_resolved",
            )
        audit.record(
            verb=row.verb, decision="rejected", caller=caller,
            args=dict(row.args or {}), confirmation_id=str(row.id),
        )
        return None

    # Claim atomically so two approvals cannot double-execute.
    claimed = PendingAction.objects.filter(
        pk=row.pk, status=PendingAction.STATUS_PENDING
    ).update(status=PendingAction.STATUS_EXECUTED, resolved_at=now, resolved_by=approved_by)
    if not claimed:
        raise ConfirmationInvalid(
            f"confirmation {row.id} was resolved concurrently",
            reason="confirmation_resolved",
        )
    try:
        return invoke(row.verb, dict(row.args or {}), caller=caller, _pending=row)
    except Exception:
        PendingAction.objects.filter(pk=row.pk).update(status=PendingAction.STATUS_FAILED)
        raise


__all__ = ["confirm", "invoke"]
