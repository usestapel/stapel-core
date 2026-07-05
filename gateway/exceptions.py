"""Gateway exceptions — every refusal has a machine-readable ``reason``.

The reason codes are part of the audit contract: the audit line for a
denied call carries ``exc.reason`` verbatim, so log consumers can slice
refusals without parsing prose. HTTP/comm adapters map exception types to
transport-level status codes.
"""
from __future__ import annotations


class GatewayError(Exception):
    """Base class for every gateway refusal/failure."""

    reason: str = "gateway_error"

    def __init__(self, message: str = "", *, reason: str | None = None) -> None:
        super().__init__(message or self.reason)
        if reason is not None:
            self.reason = reason


class VerbNotDeclared(GatewayError):
    """Deny-by-default: the verb is not in the merged registry — it does
    not exist. Adapters answer 404 (no capability enumeration)."""

    reason = "verb_not_declared"


class ArgsInvalid(GatewayError):
    """Arguments violate the verb's JSON schema (S5: container input is
    untrusted; nothing unvalidated reaches a handler)."""

    reason = "args_invalid"


class PolicyDenied(GatewayError):
    """The policy engine refused the call (tier, custom checks...)."""

    reason = "policy_denied"


class RateLimited(PolicyDenied):
    """The verb's rate limit for this caller is exhausted."""

    reason = "rate_limited"


class TokenInvalid(GatewayError):
    """Scope token missing / unknown / expired / revoked / wrong project."""

    reason = "token_invalid"


class NetworkMismatch(GatewayError):
    """Network identity check failed: a request about project X did not
    come from the network bound to project X's token."""

    reason = "network_mismatch"


class ConfirmationInvalid(GatewayError):
    """Confirmation id unknown, already resolved, or expired."""

    reason = "confirmation_invalid"


class HandlerError(GatewayError):
    """The verb handler raised. The call is audited as executed/failed."""

    reason = "handler_error"


class AuditFailure(GatewayError):
    """The audit sink raised. Fail-closed and fail-noisy (S6): a privileged
    call must never complete silently unaudited."""

    reason = "audit_failure"


class GatewayConfigError(GatewayError):
    """The gateway itself is misconfigured (bad handler path, schema
    validation impossible...). Fails closed, never open."""

    reason = "gateway_misconfigured"


__all__ = [
    "ArgsInvalid",
    "AuditFailure",
    "ConfirmationInvalid",
    "GatewayConfigError",
    "GatewayError",
    "HandlerError",
    "NetworkMismatch",
    "PolicyDenied",
    "RateLimited",
    "TokenInvalid",
    "VerbNotDeclared",
]
