"""Policy engine — the extensible allow/deny brain of the gateway.

The engine answers one question per call: *may this caller run this verb
right now?* It raises :class:`PolicyDenied` (or a subclass) with a
machine-readable reason; it never returns a "maybe". The service layer
audits every denial (S6).

Extensibility: ``STAPEL_GATEWAY["POLICY_ENGINE"]`` is a dotted path to a
:class:`PolicyEngine` subclass. The default checks tiers and rate limits;
a deployment engine typically ``super().check(...)`` first and layers its
own rules (budget caps, freeze windows, per-verb business gates) on top.

Safe defaults, spelled out:

- a verb restricted by ``tiers`` with an *unresolvable* caller tier is a
  **denial**, not a pass (fail-closed);
- a malformed ``rate_limit`` string is a configuration error, not
  "unlimited";
- confirmation is not the engine's business — the service layer parks the
  call *after* the engine allowed it, so a confirmed execution re-passes
  the same checks.
"""
from __future__ import annotations

from .base import CallerContext, VerbDeclaration
from .conf import gateway_settings
from .exceptions import GatewayConfigError, PolicyDenied, RateLimited
from .ratelimit import RateLimiter, parse_rate


class PolicyEngine:
    """Base engine: no checks. Subclass and override :meth:`check`."""

    def check(self, declaration: VerbDeclaration, args: dict, caller: CallerContext) -> None:
        """Raise :class:`PolicyDenied` to refuse; return to allow."""


class DefaultPolicyEngine(PolicyEngine):
    """Tier gate + rate limit."""

    def check(self, declaration: VerbDeclaration, args: dict, caller: CallerContext) -> None:
        self.check_tier(declaration, caller)
        self.check_rate(declaration, caller)

    # -- tiers ---------------------------------------------------------

    def resolve_tier(self, caller: CallerContext) -> str | None:
        if caller.tier:
            return caller.tier
        resolver = gateway_settings.TIER_RESOLVER
        if resolver is not None and caller.project:
            return resolver(caller.project)
        return None

    def check_tier(self, declaration: VerbDeclaration, caller: CallerContext) -> None:
        allowed = declaration.policy.tiers
        if allowed is None:
            return
        tier = self.resolve_tier(caller)
        if tier is None:
            raise PolicyDenied(
                f"verb {declaration.name!r} is tier-restricted and the caller's "
                "tier could not be resolved",
                reason="tier_unresolved",
            )
        if tier not in allowed:
            raise PolicyDenied(
                f"verb {declaration.name!r} is not available on tier {tier!r}",
                reason="tier_denied",
            )

    # -- rate limit ----------------------------------------------------

    def rate_limiter(self) -> RateLimiter:
        limiter = gateway_settings.RATE_LIMITER
        if isinstance(limiter, type):
            limiter = limiter()
        if not isinstance(limiter, RateLimiter):
            raise GatewayConfigError(
                f"STAPEL_GATEWAY['RATE_LIMITER'] resolved to {limiter!r}, "
                "which is not a RateLimiter"
            )
        return limiter

    def check_rate(self, declaration: VerbDeclaration, caller: CallerContext) -> None:
        rate = declaration.policy.rate_limit
        if not rate:
            return
        limit, window = parse_rate(rate)
        if not self.rate_limiter().allow(declaration.name, caller, limit=limit, window=window):
            raise RateLimited(
                f"verb {declaration.name!r} exceeded {rate} for "
                f"project {caller.project or '-'}"
            )


def get_policy_engine() -> PolicyEngine:
    engine = gateway_settings.POLICY_ENGINE
    if isinstance(engine, type):
        engine = engine()
    if not isinstance(engine, PolicyEngine):
        raise GatewayConfigError(
            f"STAPEL_GATEWAY['POLICY_ENGINE'] resolved to {engine!r}, "
            "which is not a PolicyEngine"
        )
    return engine


__all__ = ["DefaultPolicyEngine", "PolicyEngine", "get_policy_engine"]
