"""System checks for the verification grant namespace (tag ``stapel_verification``).

Challenges, grants and verification tokens live in a FLEET-WIDE cache
namespace (``grants.py``, :mod:`stapel_core.core.fleet_cache`) so that a
step-up completed in the auth service counts in the peer service whose gate
demanded it. Two ways a deployment can defeat that, both silent, both the
mirror image of the revocation findings in
:mod:`stapel_core.django.blacklist_checks`.
"""
from __future__ import annotations

from django.core import checks

from stapel_core.django.check_guard import (
    SecurityCriticalWarning,
    declare_security_critical,
)

#: Security-critical: a namespace that is not shared is a step-up that does
#: not travel, and the failure is silent on both sides — the auth service
#: reports the factor completed, the peer keeps demanding it (or, in the
#: admin gate, demands a grant nothing reachable can mint).
W001_GRANT_NAMESPACE_CUSTOM = declare_security_critical(
    "stapel_core.verification.W001",
    "a per-service verification namespace is the per-service grant defect "
    "with extra steps: peers compute a different key and never see the grant",
)
E001_GRANT_CACHE_MISSING = declare_security_critical(
    "stapel_core.verification.E001",
    "the verification namespace names a cache alias this deployment does not "
    "define, so every grant is written nowhere and step-up can never pass",
)
W002_GRANT_CACHE_UNDEFINED = "stapel_core.verification.W002"


@checks.register("stapel_verification")
def check_grant_namespace(app_configs=None, **kwargs):
    """E001/W001 — the shared namespace has to actually be shared."""
    from django.conf import settings

    from .grants import (
        DEFAULT_GRANT_NAMESPACE,
        grant_cache_alias,
        grant_namespace,
    )

    findings = []
    caches = getattr(settings, "CACHES", None) or {}
    alias = grant_cache_alias()

    if alias not in caches and "default" not in caches:
        findings.append(checks.Error(
            f"STAPEL_VERIFICATION['GRANT_CACHE'] names cache alias {alias!r}, "
            "which is not defined in CACHES, and there is no 'default' to fall "
            "back to. Verification grants are written nowhere, so every "
            "step-up-gated operation is permanently refused.",
            hint="Define the alias in CACHES, or drop the key and let grants "
                 "use the default cache.",
            id=E001_GRANT_CACHE_MISSING,
        ))
    elif alias not in caches:
        findings.append(checks.Warning(
            f"STAPEL_VERIFICATION['GRANT_CACHE'] names cache alias {alias!r}, "
            "which is not defined in CACHES; grants fall back to 'default'.",
            hint="Define the alias, or drop the key to say so explicitly.",
            id=W002_GRANT_CACHE_UNDEFINED,
        ))

    namespace = grant_namespace()
    if namespace != DEFAULT_GRANT_NAMESPACE:
        findings.append(SecurityCriticalWarning(
            f"STAPEL_VERIFICATION['GRANT_NAMESPACE'] is {namespace!r}, not the "
            f"fleet default {DEFAULT_GRANT_NAMESPACE!r}. A verification grant "
            "only travels between services that agree on this value — a peer "
            "left on the default will not see step-up completed here, and this "
            "service will not see step-up completed there. The admin step-up "
            "gate then asks for a grant the operator has no way to produce.",
            hint="Set the identical value in EVERY service of this fleet, or "
                 "remove it everywhere and use the default. Use a custom "
                 "namespace only to run two independent fleets against one "
                 "store.",
            id=W001_GRANT_NAMESPACE_CUSTOM,
        ))

    return findings


__all__ = [
    "E001_GRANT_CACHE_MISSING",
    "W001_GRANT_NAMESPACE_CUSTOM",
    "W002_GRANT_CACHE_UNDEFINED",
    "check_grant_namespace",
]
