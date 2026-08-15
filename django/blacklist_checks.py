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
