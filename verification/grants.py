"""Challenge and grant stores (cache-backed, TTL-scoped).

Challenge: short-lived record created when a protected view rejects a
request — identifies user, scope and the factor set that may satisfy it.
Grant: proof that the user completed a factor for a scope recently; the
protected view accepts the request while the grant is fresh.

Both live in the Django cache: they are ephemeral by design and must be
shared across workers (use Redis in production, as the framework default
cache already is).

**The namespace is fleet-wide, not per-service (0.45.0).** Until 0.44.1 these
records went through ``django.core.cache.cache``, which Django keys under the
*deployment's* ``KEY_PREFIX`` — a value every service in a split deployment
sets differently on purpose. So a step-up completed in the auth service wrote
``auth:1:stapel:verification:grant:<uid>:sensitive`` while the profiles
service looked for ``stapel_profiles:1:stapel:verification:grant:<uid>:...``,
found nothing, and demanded the factor again — or, in the admin step-up gate
(``access/stepup.py``), demanded a grant the operator had no way to produce
from that process at all. Revocation had already met and fixed this exact
defect; grants now use the same mechanism
(:mod:`stapel_core.core.fleet_cache`), which borrows the deployment's own
cache connection and forces ``KEY_PREFIX``/``VERSION`` to fleet values.

User identity is fleet-stable, so the key is too: ``user.pk`` is the UUID the
JWT ``user_id`` claim carries, and consumer-mode services materialise the
local row under that same pk (``load_user_by_uid``).

Configuration lives in ``STAPEL_VERIFICATION`` and, like the revocation pair,
must match across peers if changed:

* ``GRANT_CACHE`` — cache alias to borrow the connection from. Default
  ``"default"``.
* ``GRANT_NAMESPACE`` — the shared key prefix. Default
  ``"stapel_verification"``. Change it only to run two independent fleets
  against one store, and then change it in EVERY service of that fleet.
  ``stapel_core.verification.checks`` reports a non-default value at boot.
"""
from __future__ import annotations

import secrets
import time
from typing import Any

from stapel_core.core.fleet_cache import fleet_cache

from .conf import verification_settings

CHALLENGE_KEY = "stapel:verification:challenge:{challenge_id}"
GRANT_KEY = "stapel:verification:grant:{user_id}:{scope}"
TOKEN_KEY = "stapel:verification:token:{token}"

#: The fleet-wide default prefix. Deliberately not derived from SERVICE_NAME,
#: DATABASE, or anything else that differs between peers.
DEFAULT_GRANT_NAMESPACE = "stapel_verification"

ERR_403_VERIFICATION_REQUIRED = "error.403.verification_required"
ERR_403_VERIFICATION_ENROLLMENT = "error.403.verification_enrollment_required"
ERR_400_VERIFICATION_FACTOR = "error.400.verification_invalid_factor"
ERR_400_VERIFICATION_FAILED = "error.400.verification_failed"
ERR_404_VERIFICATION_CHALLENGE = "error.404.verification_challenge_not_found"
ERR_423_VERIFICATION_LOCKED = "error.423.verification_locked"


def grant_namespace() -> str:
    """The shared key prefix this deployment writes challenges/grants under."""
    return str(verification_settings.GRANT_NAMESPACE or DEFAULT_GRANT_NAMESPACE)


def grant_cache_alias() -> str:
    """Which ``CACHES`` entry the verification connection is built from."""
    return str(verification_settings.GRANT_CACHE or "default")


def _cache():
    """The fleet-shared connection every challenge, grant and token uses.

    Not ``django.core.cache.cache``: that is namespaced per service, and a
    grant only one service can see is a grant the fleet cannot honour.
    """
    return fleet_cache(
        namespace=grant_namespace(),
        alias=grant_cache_alias(),
        what="verification grant",
    )


# ---------------------------------------------------------------------------
# Challenges
# ---------------------------------------------------------------------------


def create_challenge(user, scope: str, factors: list[str], max_age: int) -> dict:
    """Create and persist a challenge; returns the client-facing record."""
    from .factors import factor_registry

    available = factor_registry.available_for(user, factors)
    challenge = {
        "challenge_id": "chg_" + secrets.token_urlsafe(24),
        "user_id": str(user.pk),
        "scope": scope,
        "factors": available or factors,
        "max_age": max_age,
        "attempts": 0,
        "expires_at": int(time.time()) + int(verification_settings.CHALLENGE_TTL),
    }
    _cache().set(
        CHALLENGE_KEY.format(challenge_id=challenge["challenge_id"]),
        challenge,
        timeout=int(verification_settings.CHALLENGE_TTL),
    )
    return challenge


def get_challenge(challenge_id: str) -> dict | None:
    return _cache().get(CHALLENGE_KEY.format(challenge_id=challenge_id))


def record_failed_attempt(challenge: dict) -> bool:
    """Bump the attempt counter; returns False when the challenge is dead."""
    challenge["attempts"] = int(challenge.get("attempts", 0)) + 1
    key = CHALLENGE_KEY.format(challenge_id=challenge["challenge_id"])
    if challenge["attempts"] >= int(verification_settings.MAX_ATTEMPTS):
        _cache().delete(key)
        return False
    ttl = max(1, challenge["expires_at"] - int(time.time()))
    _cache().set(key, challenge, timeout=ttl)
    return True


def complete_challenge(challenge: dict) -> str:
    """Consume the challenge, write the grant, mint a stateless token."""
    _cache().delete(CHALLENGE_KEY.format(challenge_id=challenge["challenge_id"]))
    grant_verification(
        user_id=challenge["user_id"],
        scope=challenge["scope"],
        max_age=int(challenge.get("max_age") or verification_settings.DEFAULT_MAX_AGE),
    )
    token = "vt_" + secrets.token_urlsafe(24)
    _cache().set(
        TOKEN_KEY.format(token=token),
        {"user_id": challenge["user_id"], "scope": challenge["scope"]},
        timeout=int(challenge.get("max_age") or verification_settings.DEFAULT_MAX_AGE),
    )
    return token


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------


def grant_verification(*, user_id: str, scope: str, max_age: int) -> None:
    _cache().set(
        GRANT_KEY.format(user_id=user_id, scope=scope),
        {"granted_at": int(time.time())},
        timeout=max_age,
    )


def has_grant(user, scope: str, *, token: str | None = None) -> bool:
    """Server-side grant OR a valid stateless token for this user+scope."""
    if _cache().get(GRANT_KEY.format(user_id=str(user.pk), scope=scope)):
        return True
    if token:
        data = _cache().get(TOKEN_KEY.format(token=token))
        if data and data.get("user_id") == str(user.pk) and data.get("scope") == scope:
            return True
    return False


def revoke_grants(user_id: str, scopes: list[str]) -> None:
    for scope in scopes:
        _cache().delete(GRANT_KEY.format(user_id=user_id, scope=scope))


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def verification_error_payload(challenge: dict) -> dict[str, Any]:
    """The structured 403 body clients build their factor UI from."""
    from . import errors  # noqa: F401 — lazy i18n key registration (needs Django)

    return {
        "localizable_error": ERR_403_VERIFICATION_REQUIRED,
        "error": "Additional verification required",
        "verification": {
            "challenge_id": challenge["challenge_id"],
            "scope": challenge["scope"],
            "factors": challenge["factors"],
            "expires_at": challenge["expires_at"],
        },
    }


def verification_enrollment_payload(scope: str, factors: list[str]) -> dict[str, Any]:
    """The 403 body for a strict endpoint hit by a user with no usable factors.

    Same envelope shape as :func:`verification_error_payload`, but there is
    nothing to verify yet — no challenge is stored, so the ``verification``
    object carries ``"enroll": true`` and the endpoint's factor list (the
    factors the user could enroll) instead of ``challenge_id``/``expires_at``.
    """
    from . import errors  # noqa: F401 — lazy i18n key registration (needs Django)

    return {
        "localizable_error": ERR_403_VERIFICATION_ENROLLMENT,
        "error": "Verification factor enrollment required",
        "verification": {
            "scope": scope,
            "factors": factors,
            "enroll": True,
        },
    }
