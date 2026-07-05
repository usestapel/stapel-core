"""stapel_core.gateway — the privilege gateway mechanism (OSS).

The security primitive behind "the agent gets the *capability*, never the
*credentials*" (system-design §5.9; studio-design §2.3). Untrusted code in
a project container talks to one known endpoint with a narrow set of
declared **verbs**; every key, password and script stays behind the
gateway in the control plane. The concrete verbs and their policies are
the deployment's business (Studio's are private) — this module ships the
mechanism only.

Threat model in one paragraph: the container is assumed hostile (S5 — a
prompt-injected agent, a malicious dependency). It cannot call what is
not declared (deny-by-default registry), cannot pass unvalidated input
(mandatory JSON-schema check), cannot speak without a live project-scoped
token (opaque, hashed at rest, short-lived, instantly revocable), cannot
speak *about* another project (token scope + network identity check),
cannot outrun its quota (per-(verb, project) rate limit), cannot trigger a
destructive verb alone (two-phase confirmation happens out of its reach),
and cannot act invisibly (every attempt — allowed, denied, parked —
lands on the audit stream, fail-closed; S6).

Quick tour::

    from stapel_core import gateway

    # Declare (AppConfig.ready() — or via STAPEL_GATEWAY["VERBS"]):
    @gateway.verb("send_email", schema={...}, policy={"rate_limit": "30/h"})
    def send_email(args, caller): ...

    # Issue the container its token (container-manager, at start):
    issued = gateway.issue_token("proj-1", container="c-1", network="10.0.7.4")

    # Container-side door: gateway.get_gateway_urls() in urls.py.
    # Control-plane door: comm call("gateway.invoke", {...}).
    # Direct: gateway.invoke("send_email", {...}, caller=CallerContext(...))
"""
from .base import CallerContext, PendingConfirmation, VerbDeclaration, VerbPolicy
from .exceptions import (
    ArgsInvalid,
    AuditFailure,
    ConfirmationInvalid,
    GatewayConfigError,
    GatewayError,
    HandlerError,
    NetworkMismatch,
    PolicyDenied,
    RateLimited,
    TokenInvalid,
    VerbNotDeclared,
)
from .http import GatewayInvokeView, get_gateway_urls
from .policy import DefaultPolicyEngine, PolicyEngine
from .ratelimit import CacheRateLimiter, RateLimiter
from .registry import register_verb, verb, verb_registry
from .service import confirm, invoke
from .tokens import (
    IssuedToken,
    issue_token,
    purge_expired_tokens,
    revoke_token,
    rotate_token,
    verify_token,
)

__all__ = [
    "ArgsInvalid",
    "AuditFailure",
    "CacheRateLimiter",
    "CallerContext",
    "ConfirmationInvalid",
    "DefaultPolicyEngine",
    "GatewayConfigError",
    "GatewayError",
    "GatewayInvokeView",
    "HandlerError",
    "IssuedToken",
    "NetworkMismatch",
    "PendingConfirmation",
    "PolicyDenied",
    "PolicyEngine",
    "RateLimited",
    "RateLimiter",
    "TokenInvalid",
    "VerbDeclaration",
    "VerbNotDeclared",
    "VerbPolicy",
    "confirm",
    "get_gateway_urls",
    "invoke",
    "issue_token",
    "purge_expired_tokens",
    "register_verb",
    "revoke_token",
    "rotate_token",
    "verb",
    "verb_registry",
    "verify_token",
]
