"""System checks for the revocation escape hatch (tag ``stapel_blacklist``).

Both blacklists in this library — ``stapel_core.core.token_blacklist`` (per
token) and ``stapel_core.django.jwt.authentication`` (per user) — fail CLOSED
when their store is unreachable, and both read one setting to be talked out of
it: ``STAPEL_BLACKLIST_FAIL_OPEN``.

That setting is a deliberate, documented trade (availability over revocation),
but it is invisible once set: nothing in a running system says "revocation is
advisory here". A deployment that flipped it during an incident and never
flipped it back looks exactly like one that never had it. So the hatch reports
itself at every boot smoke.

W-level, not E: choosing availability is a legitimate stance the operator is
entitled to hold. The check exists so the stance stays a choice instead of
becoming forgotten configuration.
"""
from __future__ import annotations

from django.core import checks

from stapel_core.django.check_guard import (
    SecurityCriticalWarning,
    declare_security_critical,
)

#: Security-critical although it is only a Warning: the whole value of
#: this finding is that a stated escape hatch stays visible, and a blanket
#: silencing line is exactly how it stops being.
W001_BLACKLIST_FAIL_OPEN = declare_security_critical(
    "stapel_core.blacklist.W001",
    "with the hatch open, revoked tokens and banned users are accepted "
    "whenever the store is unreachable",
)
W002_BLACKLIST_LOCMEM = "stapel_core.blacklist.W002"

#: The one cache backend that cannot hold a fleet-wide revocation.
_LOCMEM_BACKEND = "django.core.cache.backends.locmem.LocMemCache"


@checks.register("stapel_blacklist")
def check_blacklist_fail_open(app_configs=None, **kwargs):
    from django.conf import settings

    if not getattr(settings, "STAPEL_BLACKLIST_FAIL_OPEN", False):
        return []

    return [SecurityCriticalWarning(
        "STAPEL_BLACKLIST_FAIL_OPEN is on: when the blacklist store is "
        "unreachable, revoked tokens and banned users are accepted instead "
        "of rejected. Ban and force-logout are advisory in this deployment.",
        hint="Remove the setting (or set it to False) to fail closed, which "
             "is the default. Keep it on only where an unreachable cache "
             "must not lock every user out, and make sure the store's "
             "availability is monitored.",
        id=W001_BLACKLIST_FAIL_OPEN,
    )]


@checks.register("stapel_blacklist")
def check_blacklist_store_is_shared(app_configs=None, **kwargs):
    """W002 — a per-process cache makes revocation per-process.

    Both blacklists write to the default Django cache. With ``LocMemCache``
    that store lives inside one worker: a logout served by worker 3 revokes
    the token in worker 3 and nowhere else, so the very next request — load
    balanced to worker 1 — authenticates the token the user just killed.
    Nothing surfaces this; the revocation call returns success either way.

    Now that revocation is enforced inside the validation seam, this is the
    difference between "revocation works" and "revocation works one time in
    N", so an operator should read it once, at boot.

    W-level and DEBUG-exempt: single-process development and the test suite
    run on LocMem legitimately, and the finding there would be noise that
    trains people to ignore the check.
    """
    from django.conf import settings

    if getattr(settings, "DEBUG", False):
        return []

    caches = getattr(settings, "CACHES", None) or {}
    default = caches.get("default") or {}
    if default.get("BACKEND") != _LOCMEM_BACKEND:
        return []

    return [checks.Warning(
        "The default cache is LocMemCache, which is per-process: revoking a "
        "token or banning a user only takes effect in the worker that handled "
        "the request. Every other worker keeps accepting the revoked token "
        "until it expires on its own.",
        hint="Point the default cache at a shared store (Redis/Memcached) so "
             "a revocation reaches every worker. If this process really is a "
             "single worker that shares nothing, set DEBUG or accept the "
             "warning knowingly.",
        id=W002_BLACKLIST_LOCMEM,
    )]


#: Security-critical: a namespace that is not shared is revocation that does
#: not propagate, and the failure is silent on both sides — the revoking
#: service reports success, the verifying service reports 200.
E001_REVOCATION_CACHE_MISSING = declare_security_critical(
    "stapel_core.revocation.E001",
    "the revocation namespace names a cache alias this deployment does not "
    "define, so every ban and every logout is written nowhere",
)
W003_REVOCATION_NAMESPACE_CUSTOM = declare_security_critical(
    "stapel_core.revocation.W003",
    "a per-service revocation namespace is the per-service blacklist defect "
    "with extra steps: peers compute a different key and never see the ban",
)


@checks.register("stapel_blacklist")
def check_revocation_namespace(app_configs=None, **kwargs):
    """E001/W003 — the shared namespace has to actually be shared.

    Both blacklists write through ``stapel_core.core.revocation_store``, which
    borrows the deployment's cache connection but forces a fleet-wide
    ``KEY_PREFIX``. Two ways a deployment can defeat that, both silent:

    * ``STAPEL_JWT_REVOCATION_CACHE`` names an alias that is not in ``CACHES``
      — the store falls back to ``default``, or to nothing;
    * ``STAPEL_JWT_REVOCATION_NAMESPACE`` is set to a non-default value. That
      is legitimate (two fleets, one Redis) but only if EVERY peer sets the
      identical value. A namespace that differs per service reproduces the
      original defect exactly, with a setting that looks deliberate.
    """
    from django.conf import settings

    from stapel_core.core.revocation_store import (
        DEFAULT_NAMESPACE,
        revocation_cache_alias,
        revocation_namespace,
    )

    findings = []
    caches = getattr(settings, "CACHES", None) or {}
    alias = revocation_cache_alias()

    if alias not in caches and "default" not in caches:
        findings.append(checks.Error(
            f"STAPEL_JWT_REVOCATION_CACHE names cache alias {alias!r}, which "
            "is not defined in CACHES, and there is no 'default' to fall back "
            "to. Token revocation and user bans are written nowhere.",
            hint="Define the alias in CACHES, or drop the setting and let "
                 "revocation use the default cache.",
            id=E001_REVOCATION_CACHE_MISSING,
        ))
    elif alias not in caches:
        findings.append(checks.Warning(
            f"STAPEL_JWT_REVOCATION_CACHE names cache alias {alias!r}, which "
            "is not defined in CACHES; revocation falls back to 'default'.",
            hint="Define the alias, or drop the setting to say so explicitly.",
            id="stapel_core.revocation.W004",
        ))

    namespace = revocation_namespace()
    if namespace != DEFAULT_NAMESPACE:
        findings.append(SecurityCriticalWarning(
            f"STAPEL_JWT_REVOCATION_NAMESPACE is {namespace!r}, not the "
            f"fleet default {DEFAULT_NAMESPACE!r}. Revocation only propagates "
            "between services that agree on this value — a peer left on the "
            "default will not see this service's bans, and this service will "
            "not see the peer's.",
            hint="Set the identical value in EVERY service that verifies "
                 "tokens signed by this key, or remove it everywhere and use "
                 "the default. Use a custom namespace only to run two "
                 "independent fleets against one store.",
            id=W003_REVOCATION_NAMESPACE_CUSTOM,
        ))

    return findings


#: Security-critical: a tombstone that expires before the credentials naming
#: the dead account do is a deletion with a resurrection window at the end.
E002_TOMBSTONE_TTL_TOO_SHORT = declare_security_critical(
    "stapel_core.revocation.E002",
    "a deletion tombstone shorter than the refresh-token lifetime leaves a "
    "window in which a deleted user's own token re-creates them",
)


@checks.register("stapel_blacklist")
def check_tombstone_ttl(app_configs=None, **kwargs):
    """E002 — the tombstone must outlive the longest credential it must refuse.

    ``STAPEL_JWT_TOMBSTONE_TTL`` defaults to ``JWT_REFRESH_TOKEN_LIFETIME``,
    so this can only fire when a deployment set it explicitly and set it too
    low — or raised the refresh lifetime afterwards and left the tombstone
    behind, which is the failure this check really exists for: the two
    numbers drift apart silently, months apart, and the deployment that
    lengthened its refresh tokens is exactly the one that most needs longer
    tombstones.

    Error, not Warning: unlike the fail-open hatch, there is no stance a
    deployment can hold here. A tombstone that ends before the credential
    does is not a trade-off, it is an unclosed hole with a number on it.
    """
    from django.conf import settings

    from stapel_core.django.jwt.tombstone import refresh_token_lifetime

    configured = getattr(settings, "STAPEL_JWT_TOMBSTONE_TTL", None)
    if configured is None:
        return []

    try:
        configured = int(configured)
    except (TypeError, ValueError):
        return [checks.Error(
            f"STAPEL_JWT_TOMBSTONE_TTL is {configured!r}, which is not a "
            "number of seconds.",
            hint="Set it to an integer >= JWT_REFRESH_TOKEN_LIFETIME, or "
                 "remove it and let it derive from that setting.",
            id=E002_TOMBSTONE_TTL_TOO_SHORT,
        )]

    refresh = refresh_token_lifetime()
    if configured >= refresh:
        return []

    return [checks.Error(
        f"STAPEL_JWT_TOMBSTONE_TTL is {configured}s but "
        f"JWT_REFRESH_TOKEN_LIFETIME is {refresh}s. For {refresh - configured}s "
        "after the tombstone expires, a deleted user's own refresh token is "
        "still valid and a consumer-mode service will re-create them from it.",
        hint=f"Raise it to at least {refresh}, or remove the setting and let "
             "it derive from JWT_REFRESH_TOKEN_LIFETIME automatically.",
        id=E002_TOMBSTONE_TTL_TOO_SHORT,
    )]
