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
A second cache *connection* is built from the deployment's own ``CACHES``
entry — same backend, same ``LOCATION``, same ``OPTIONS``, so the same Redis
and the same pool settings — with ``KEY_PREFIX`` and ``VERSION`` forced to
values that are a property of the FLEET, not of the service. Any peer that
runs this library and points at the same store computes the same key.

``KEY_FUNCTION`` is dropped for the same reason: a per-service key function
would re-isolate the namespace this module exists to share.

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

logger = logging.getLogger(__name__)

#: The fleet-wide default. Deliberately not derived from SERVICE_NAME,
#: DATABASE, or anything else that differs between peers.
DEFAULT_NAMESPACE = "stapel_revocation"

#: Pinned: the namespace is a wire format between services, so bumping it is
#: a fleet-wide migration, never an incidental per-service value.
NAMESPACE_VERSION = 1

_CONNECTIONS: dict = {}


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


def _build(alias: str, namespace: str):
    from django.conf import settings
    from django.utils.module_loading import import_string

    caches = getattr(settings, "CACHES", None) or {}
    conf = caches.get(alias)
    if conf is None:
        conf = caches.get("default")
    if not conf:
        raise KeyError(f"no CACHES entry for alias {alias!r} (and no default)")

    params = dict(conf)
    backend_path = params.pop("BACKEND")
    location = params.pop("LOCATION", "")
    # The three keys that decide the final Redis key, forced to fleet values.
    params["KEY_PREFIX"] = namespace
    params["VERSION"] = NAMESPACE_VERSION
    params.pop("KEY_FUNCTION", None)

    backend_cls = import_string(backend_path)
    return backend_cls(location, params)


def revocation_cache():
    """A cache connection into the shared revocation namespace.

    Falls back to the ordinary ``caches[alias]`` connection if a deployment's
    backend cannot be re-instantiated this way. That fallback is the OLD,
    prefix-scoped behaviour, so it is logged at error level rather than
    passed over: a deployment on such a backend has per-service revocation
    and needs to know.
    """
    alias = revocation_cache_alias()
    namespace = revocation_namespace()
    key = (alias, namespace)

    hit = _CONNECTIONS.get(key)
    if hit is not None:
        return hit

    try:
        store = _build(alias, namespace)
    except Exception as exc:  # pragma: no cover - backend-specific
        logger.error(
            "Cannot open the shared revocation namespace on cache %r (%s); "
            "falling back to this service's own cache prefix, which means "
            "revocation does NOT propagate to peer services.",
            alias,
            exc,
        )
        from django.core.cache import caches

        return caches[alias]

    _CONNECTIONS[key] = store
    return store


def reset_revocation_cache(**kwargs) -> None:
    """Drop memoized connections (settings changed, or a test overrode them)."""
    _CONNECTIONS.clear()


def _connect_settings_reset() -> None:
    """Rebuild on ``override_settings``, so tests and reloads see new CACHES."""
    try:
        from django.test.signals import setting_changed
    except Exception:  # pragma: no cover - django.test not importable
        return
    setting_changed.connect(reset_revocation_cache, dispatch_uid="stapel_revocation_reset")


_connect_settings_reset()
