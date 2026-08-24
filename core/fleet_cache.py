"""One cache namespace shared by every service of a fleet.

Why this is a mechanism and not a helper
----------------------------------------
Django builds the real cache key as ``f"{KEY_PREFIX}:{VERSION}:{key}"`` from
the *deployment's* ``CACHES`` entry, and every service in a split deployment
sets its own ``KEY_PREFIX`` (``auth``, ``stapel_profiles``, ...) precisely so
that its ordinary caches do not collide with its peers'.

Some state must collide. Revocation was the first (0.39.0): the auth service
wrote ``auth:1:jwt_blacklist:<jti>``, the profiles service looked for
``stapel_profiles:1:jwt_blacklist:<jti>``, found nothing, and served the
request — "log out everywhere" was a per-service illusion. Verification
grants are the second (0.45.0): a step-up completed in the auth service was
invisible to the peer whose admin gate demanded it, so the gate's own
documented property ("completing step-up anywhere satisfies it") held only
inside one ``KEY_PREFIX``.

Both are the same defect, so they get the same mechanism rather than a second
one. This module is that mechanism, named for what it does — fleet-shared
state — instead of for the first caller that needed it.

How
---
A second cache *connection* is built from the deployment's own ``CACHES``
entry — same backend, same ``LOCATION``, same ``OPTIONS``, so the same Redis
and the same pool settings — with ``KEY_PREFIX`` and ``VERSION`` forced to
values that are a property of the FLEET, not of the service. Any peer that
runs this library and points at the same store computes the same key.

``KEY_FUNCTION`` is dropped for the same reason: a per-service key function
would re-isolate the namespace this module exists to share.

A namespace is a wire format between services. Every caller therefore
declares a *default* namespace that is deliberately not derived from
SERVICE_NAME, DATABASE or anything else that differs between peers, and any
deployment that overrides it must override it in EVERY peer — which is why
each caller also ships a boot-time system check that reports a non-default
value (``stapel_core.revocation.W003``, ``stapel_core.verification.W001``).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Pinned: the namespace is a wire format between services, so bumping it is
#: a fleet-wide migration, never an incidental per-service value.
NAMESPACE_VERSION = 1

_CONNECTIONS: dict = {}


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


def fleet_cache(*, namespace: str, alias: str = "default", what: str = "fleet-shared"):
    """A cache connection into the shared *namespace*, memoized per pair.

    *what* names the state for the fallback log line only.

    Falls back to the ordinary ``caches[alias]`` connection if a deployment's
    backend cannot be re-instantiated this way. That fallback is the OLD,
    prefix-scoped behaviour, so it is logged at error level rather than
    passed over: a deployment on such a backend has per-service state where
    the fleet expects shared state, and needs to know.
    """
    key = (alias, namespace)

    hit = _CONNECTIONS.get(key)
    if hit is not None:
        return hit

    try:
        store = _build(alias, namespace)
    except Exception as exc:  # pragma: no cover - backend-specific
        logger.error(
            "Cannot open the shared %s namespace on cache %r (%s); falling "
            "back to this service's own cache prefix, which means this state "
            "does NOT propagate to peer services.",
            what,
            alias,
            exc,
        )
        from django.core.cache import caches

        return caches[alias]

    _CONNECTIONS[key] = store
    return store


def reset_fleet_caches(**kwargs) -> None:
    """Drop memoized connections (settings changed, or a test overrode them)."""
    _CONNECTIONS.clear()


def _connect_settings_reset() -> None:
    """Rebuild on ``override_settings``, so tests and reloads see new CACHES."""
    try:
        from django.test.signals import setting_changed
    except Exception:  # pragma: no cover - django.test not importable
        return
    setting_changed.connect(reset_fleet_caches, dispatch_uid="stapel_fleet_cache_reset")


_connect_settings_reset()


__all__ = [
    "NAMESPACE_VERSION",
    "fleet_cache",
    "reset_fleet_caches",
]
