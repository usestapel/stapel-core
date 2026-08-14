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

W001_BLACKLIST_FAIL_OPEN = "stapel_core.blacklist.W001"


@checks.register("stapel_blacklist")
def check_blacklist_fail_open(app_configs=None, **kwargs):
    from django.conf import settings

    if not getattr(settings, "STAPEL_BLACKLIST_FAIL_OPEN", False):
        return []

    return [checks.Warning(
        "STAPEL_BLACKLIST_FAIL_OPEN is on: when the blacklist store is "
        "unreachable, revoked tokens and banned users are accepted instead "
        "of rejected. Ban and force-logout are advisory in this deployment.",
        hint="Remove the setting (or set it to False) to fail closed, which "
             "is the default. Keep it on only where an unreachable cache "
             "must not lock every user out, and make sure the store's "
             "availability is monitored.",
        id=W001_BLACKLIST_FAIL_OPEN,
    )]
