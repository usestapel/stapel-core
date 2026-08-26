"""Per-user verification policy — resolved from the auth service via comm.

The policy says which scopes a user turned off (for ``default_on``
endpoints) or turned on (for ``opt_in`` endpoints). The auth service owns
the storage and exposes it as the ``auth.verification.policy`` Function;
this module resolves and caches it so protected views don't pay a
cross-service round-trip on every request.

Fail-safe rules when the Function is unavailable (not registered, remote
error, timeout): ``get_user_policy`` returns ``None`` and the caller must
fall back to the SAFE side of its level — ``default_on`` keeps protection
ON (as if nothing was disabled), ``opt_in`` keeps it OFF (as if nothing was
enabled). ``scope_enforced`` implements exactly that mapping. ``strict``
never consults the policy at all.

Known gap: this cache is still service-local
--------------------------------------------
``_cache()`` here is ``django.core.cache.cache``, which Django keys under the
*deployment's* ``KEY_PREFIX``. This is the last part of
:mod:`stapel_core.verification` that did not move to the fleet-wide namespace
in 0.45.0, so an invalidation in the auth service leaves every peer serving its
own stale copy for ``POLICY_CACHE_TTL``. :func:`invalidate_policy_cache` says
so in its report (the namespace field reads ``service-local:…``) rather than
implying a reach it does not have. Closing it is a wire-format change between
peers and belongs to its own release; see the 0.47.0 CHANGELOG entry.
"""
from __future__ import annotations

import logging

from stapel_core.core.drop import DropReport, drop_cache_key, service_namespace

from .conf import verification_settings

logger = logging.getLogger(__name__)

#: The auth-owned comm Function resolving a user's policy.
POLICY_FUNCTION = "auth.verification.policy"

POLICY_KEY = "stapel:verification:policy:{user_id}"


def _cache():
    from django.core.cache import cache

    return cache


def get_user_policy(user) -> dict | None:
    """The user's verification policy, or ``None`` when it can't be resolved.

    Shape: ``{"disabled_scopes": [...], "enabled_scopes": [...]}``. Resolved
    via ``call("auth.verification.policy", {"user_id": ...})`` and cached in
    the Django cache for ``POLICY_CACHE_TTL`` seconds. Failures are NOT
    cached — the next request retries.
    """
    user_id = str(user.pk)
    key = POLICY_KEY.format(user_id=user_id)
    cached = _cache().get(key)
    if cached is not None:
        return cached

    from stapel_core.comm import call
    from stapel_core.comm.exceptions import (
        FunctionCallError,
        FunctionNotRegistered,
        FunctionRouteNotConfigured,
    )

    try:
        result = call(POLICY_FUNCTION, {"user_id": user_id}, timeout=2.0)
    except (FunctionNotRegistered, FunctionRouteNotConfigured, FunctionCallError) as exc:
        logger.warning(
            "verification policy unavailable user=%s (%s) — falling back to "
            "per-level fail-safe defaults",
            user_id, exc,
        )
        return None

    policy = {
        "disabled_scopes": list((result or {}).get("disabled_scopes") or []),
        "enabled_scopes": list((result or {}).get("enabled_scopes") or []),
    }
    _cache().set(key, policy, timeout=int(verification_settings.POLICY_CACHE_TTL))
    return policy


def invalidate_policy_cache(user_id) -> DropReport:
    """Drop the cached policy for *user_id* (call after a preference write).

    Reports what that did — read, delete, read back — instead of the ``None``
    it returned until 0.47.0.

    **Read the ``namespace`` on the report before believing it.** It says
    ``service-local:<KEY_PREFIX>``, and that is the honest scope of a
    ``DROPPED``: this process's cache, and no peer's. Every other part of
    :mod:`stapel_core.verification` moved onto the fleet-wide namespace in
    0.45.0; this one did not, so invalidating a policy in the auth service
    does NOT invalidate it in the peer that enforces it, and the peer keeps
    serving the stale answer for ``POLICY_CACHE_TTL``. Self-healing, and
    bounded — but a user who turns a scope ON can be unprotected in a peer for
    up to that window.

    That move is a wire format between peers, exactly as ``GRANT_NAMESPACE``
    was: two peers on different namespaces both read a cache neither writes.
    It therefore gets its own release rather than riding an additive one, and
    is not silently half-done here. What IS fixed here is that the scope is now
    visible in the return value instead of being invisible in a ``None``.
    """
    return drop_cache_key(
        _cache(),
        POLICY_KEY.format(user_id=str(user_id)),
        what="cached verification policy",
        namespace=service_namespace(),
        log=logger,
        hint=(
            "this cache is still SERVICE-LOCAL (unlike challenges/grants since "
            "0.45.0), so a peer's cached policy is a different record and this "
            "call cannot reach it"
        ),
    )


def scope_enforced(user, scope: str, level: str) -> bool:
    """Whether a ``default_on``/``opt_in`` endpoint enforces *scope* for *user*.

    Applies the fail-safe rules: an unresolvable policy keeps ``default_on``
    protection ON and leaves ``opt_in`` protection OFF.
    """
    if level == "opt_in":
        policy = get_user_policy(user)
        return policy is not None and scope in policy["enabled_scopes"]
    if level == "default_on":
        policy = get_user_policy(user)
        return policy is None or scope not in policy["disabled_scopes"]
    # "strict" (and anything unknown) never consults user preferences.
    return True


__all__ = [
    "POLICY_FUNCTION",
    "POLICY_KEY",
    "get_user_policy",
    "invalidate_policy_cache",
    "scope_enforced",
]
