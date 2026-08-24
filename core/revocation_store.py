"""One cache namespace for revocation, shared by every service that verifies
tokens signed by the same key.

The defect this exists to close
-------------------------------
Both blacklists used to write through ``django.core.cache.cache``. Django's
cache layer builds the real key as ``f"{KEY_PREFIX}:{VERSION}:{key}"`` from
the *deployment's* ``CACHES["default"]``, and every service in a split
deployment sets its own ``KEY_PREFIX`` (``auth``, ``stapel_profiles``, ...)
precisely so that its ordinary caches do not collide with its peers'.

Revocation is the one thing that must collide. Sharing a Redis instance is
not sharing a namespace: the auth service wrote ``auth:1:jwt_blacklist:<jti>``
and the profiles service looked for ``stapel_profiles:1:jwt_blacklist:<jti>``,
found nothing, and served the request. Reproduced on a consumer's stand: a
token revoked in auth still returned 200 from profiles. "Log out everywhere",
"revoke suspicious session" and password-change revocation were all
per-service illusions — a revoked token kept working on every service except
the one that revoked it, until it expired on its own.

The user-level ban in ``django/jwt/authentication.py`` had already met this
and worked around it by reaching for ``cache.client.get_client()`` — a raw
Redis handle that bypasses ``KEY_PREFIX``. That worked only on
``django_redis`` and silently fell back to the broken, prefix-scoped path on
every other backend. This module replaces the workaround with the mechanism,
and both blacklists use it, so the two halves of revocation cannot drift
apart again.

How
---
The mechanism itself lives in :mod:`stapel_core.core.fleet_cache` (0.45.0,
generalized out of this module when verification grants turned out to have
the identical defect — see that module's header). It builds a second cache
*connection* from the deployment's own ``CACHES`` entry — same backend, same
``LOCATION``, same ``OPTIONS`` — with ``KEY_PREFIX`` and ``VERSION`` forced to
values that are a property of the FLEET, not of the service, and drops
``KEY_FUNCTION`` so a per-service one cannot re-isolate the namespace. This
module contributes the revocation-specific half: which namespace, and which
alias to borrow the connection from.

Configuration (both optional, and both must match across peers if changed):

* ``STAPEL_JWT_REVOCATION_CACHE`` — cache alias to borrow the connection
  from. Default ``"default"``.
* ``STAPEL_JWT_REVOCATION_NAMESPACE`` — the shared key prefix. Default
  ``"stapel_revocation"``. Change it only to run two independent fleets
  against one Redis, and then change it in EVERY service of that fleet:
  a namespace set per-service is the original defect with extra steps.
  ``stapel_core.django.blacklist_checks`` reports a non-default value at
  every boot so it cannot quietly be one service's local opinion.
"""
from __future__ import annotations

import logging

from .fleet_cache import NAMESPACE_VERSION, fleet_cache, reset_fleet_caches

logger = logging.getLogger(__name__)

#: The fleet-wide default. Deliberately not derived from SERVICE_NAME,
#: DATABASE, or anything else that differs between peers.
DEFAULT_NAMESPACE = "stapel_revocation"


def revocation_namespace() -> str:
    """The shared key prefix this deployment writes revocations under."""
    from django.conf import settings

    return (
        getattr(settings, "STAPEL_JWT_REVOCATION_NAMESPACE", None)
        or DEFAULT_NAMESPACE
    )


def revocation_cache_alias() -> str:
    """Which ``CACHES`` entry the revocation connection is built from."""
    from django.conf import settings

    return getattr(settings, "STAPEL_JWT_REVOCATION_CACHE", None) or "default"


def revocation_cache():
    """A cache connection into the shared revocation namespace."""
    return fleet_cache(
        namespace=revocation_namespace(),
        alias=revocation_cache_alias(),
        what="revocation",
    )


def reset_revocation_cache(**kwargs) -> None:
    """Drop memoized connections (settings changed, or a test overrode them)."""
    reset_fleet_caches(**kwargs)


__all__ = [
    "DEFAULT_NAMESPACE",
    "NAMESPACE_VERSION",
    "reset_revocation_cache",
    "revocation_cache",
    "revocation_cache_alias",
    "revocation_namespace",
]
