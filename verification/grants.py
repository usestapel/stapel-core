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

Dropping is a measured fact, not a gesture (0.46.0)
---------------------------------------------------
Every record here is created by a named public function, and until 0.46.0 not
one of them could be *removed* by one: a consumer that needed a challenge gone
— an expired-path test, an operator killing a step-up nobody asked for, an
erasure — had to reach into ``grants._cache()`` or, worse, compute the key
itself against ``django.core.cache.cache``.

The second option was silently wrong from the moment the namespace moved. A
plain-cache delete computes ``<service>:1:stapel:verification:challenge:<id>``
while this module reads ``stapel_verification:1:...``, so it removes nothing
and cannot say anything useful about the record the caller means: Django's
``cache.delete`` answers about ITS key, not ours. stapel-auth 0.28.0 shipped a
test built on that delete — the setup did nothing at all, and the assertion
"verified" an emptiness nobody had created.

So the terminal verbs — :func:`drop_challenge`, :func:`drop_verification_token`
and :func:`revoke_grants` — do not return ``None`` and do not hand back a raw
backend boolean. Each reads the key THIS module writes, deletes it, reads it
BACK, and returns a :class:`DropReport` saying which of three different things
happened (``DROPPED`` / ``NOT_FOUND`` / ``STILL_PRESENT``). ``NOT_FOUND`` and
``STILL_PRESENT`` are also logged, naming the namespace, so a caller who
ignores the return value still cannot get a no-op quietly.

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

import logging
import secrets
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from stapel_core.core.fleet_cache import fleet_cache

from .conf import verification_settings

logger = logging.getLogger(__name__)

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
# Dropping — the terminal verb, and what it reports
# ---------------------------------------------------------------------------


class DropOutcome(str, Enum):
    """What a drop actually did to the store.

    Three different facts, and none of them may be folded into another — the
    same rule :class:`~stapel_core.verification.codes.CodeOutcome` states for
    reads. Collapsing them is precisely the defect: a delete that removed
    nothing is not a delete that worked.
    """

    #: A record was there under this key; a read-back confirms it is gone.
    DROPPED = "dropped"
    #: Nothing was stored under this key. Already spent, aged out — or the
    #: writer computed a DIFFERENT key (a different ``GRANT_NAMESPACE``, or a
    #: caller reaching for ``django.core.cache.cache`` instead of this module).
    NOT_FOUND = "not_found"
    #: The delete ran and the record is STILL readable. The store did not obey;
    #: never report this as success.
    STILL_PRESENT = "still_present"


@dataclass(frozen=True)
class DropReport:
    """The verdict of a drop, plus enough context to explain a ``NOT_FOUND``.

    Falsy unless the record was found and is now gone, so the ordinary
    ``assert drop_challenge(cid)`` is a real assertion rather than a truthy
    enum member that passes on every outcome.
    """

    outcome: DropOutcome
    #: What was being dropped: ``"challenge"``, ``"grant"``, ``"token"``.
    what: str
    #: The unprefixed key, as this module computes it.
    key: str
    #: The fleet namespace the key was computed under. The first thing to
    #: compare against the writer's when an expected record was ``NOT_FOUND``.
    namespace: str

    def __bool__(self) -> bool:
        return self.outcome is DropOutcome.DROPPED


def _drop(key: str, what: str) -> DropReport:
    """Delete *key* and report what that did, having read the store back.

    Read, delete, read again. The read-back is what makes ``DROPPED`` a
    measurement instead of a claim, and it costs one cache round-trip on a
    path that runs once per challenge at most.
    """
    cache = _cache()
    namespace = grant_namespace()
    existed = cache.get(key) is not None
    cache.delete(key)
    survived = cache.get(key) is not None

    if survived:
        outcome = DropOutcome.STILL_PRESENT
        logger.error(
            "verification %s NOT dropped: %r is still readable in namespace %r "
            "after delete — the store did not obey; do not treat this as success",
            what, key, namespace,
        )
    elif existed:
        outcome = DropOutcome.DROPPED
    else:
        outcome = DropOutcome.NOT_FOUND
        logger.warning(
            "verification %s drop found nothing at %r in namespace %r. If you "
            "expected a record here, whatever wrote it computed a different key "
            "— check GRANT_NAMESPACE/GRANT_CACHE agree with the writer, and that "
            "the writer went through stapel_core.verification and not "
            "django.core.cache.cache.",
            what, key, namespace,
        )
    return DropReport(outcome=outcome, what=what, key=key, namespace=namespace)


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


def drop_challenge(challenge_id: str) -> DropReport:
    """Drop the challenge *challenge_id*; returns what that actually did.

    Keyed by id, like :func:`get_challenge` — the id is what a client holds
    from the 403 envelope, and what a test holds from
    :func:`create_challenge`.

    This is deliberately BOTH an operational and a testing primitive, in the
    ordinary public API rather than a ``testing`` sidecar. Operations need it:
    a step-up nobody requested, an erasure, a session known to be compromised
    — the same removal :func:`record_failed_attempt` performs when the attempt
    budget runs out, triggered by a different cause. And a "for tests only"
    function that operations will reach for anyway is better named honestly
    than quarantined into a module the contract does not describe: the version
    of this seam that WAS private is the reason a consumer's release died on a
    delete that removed nothing.

    Truthy only when a challenge was there and is now gone::

        assert drop_challenge(challenge["challenge_id"])   # a real assertion
        assert get_challenge(challenge["challenge_id"]) is None
    """
    return _drop(CHALLENGE_KEY.format(challenge_id=challenge_id), "challenge")


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


def revoke_grants(user_id: str, scopes: list[str]) -> list[DropReport]:
    """Drop *user_id*'s grants for *scopes*; one report per scope, in order.

    Returned rather than dropped on the floor since 0.46.0 (it used to return
    ``None``): "revoke everywhere" is a security operation, and one that
    removed nothing must not be indistinguishable from one that worked.

    Note what this does NOT reach: a stateless verification token minted by
    :func:`complete_challenge` is keyed by the token itself, not by user, so it
    cannot be enumerated from a user id and survives this call for its full
    ``max_age``. A holder of that token still satisfies
    :func:`has_grant`. Drop it with :func:`drop_verification_token` if you hold
    it; if you do not, the only bound is its lifetime.
    """
    return [
        _drop(GRANT_KEY.format(user_id=user_id, scope=scope), "grant")
        for scope in scopes
    ]


def drop_verification_token(token: str) -> DropReport:
    """Drop the stateless verification token *token*; reports what that did.

    The counterpart of the token :func:`complete_challenge` mints. Until
    0.46.0 nothing public could remove one — not even :func:`revoke_grants`,
    which deletes grants and cannot see tokens (see its note) — so a leaked
    ``X-Verification-Token`` stayed good until its TTL ran out.
    """
    return _drop(TOKEN_KEY.format(token=token), "token")


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
