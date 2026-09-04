"""Deletion tombstones — a deleted account cannot be re-created from its own token.

The defect
----------
A consumer-mode verifier (``JWT_CREATE_USERS_FROM_TOKEN=True``) is *supposed*
to materialise a local row for a user it has never seen — that is the entire
point of the mode: identity lives in the auth service and every other service
shadow-copies it on first contact. But it cannot tell "I have never seen this
uid" from "I deleted this uid", because both look identical from inside:
``User.objects.get(pk=...)`` raises ``DoesNotExist`` either way.

So a deleted user's still-valid token walked into a consumer service and was
re-created from its own claims, and the service then served that user's
profile. Reported live: the profiles service answering for an account that no
longer existed anywhere else. Deletion is the one lifecycle event a bearer
token must never undo, and it was the one the shadow-copy design could not
represent.

The mechanism
-------------
The issuer writes a **tombstone** — "this uid is gone" — into the fleet-wide
revocation namespace built in 0.39.0 (``stapel_core.core.revocation_store``),
so every peer reads the same key regardless of its own cache ``KEY_PREFIX``.
Consumer-mode verifiers consult it *before* trusting any claim, and refuse.

Three things this deliberately is NOT:

* **Not the per-user ban.** ``blacklist_user`` was the interim remedy and it
  is the wrong shape for this: it is a moderation action someone must
  remember to take, with a TTL sized for moderation. A tombstone is a fact
  about the account, written by the deletion itself.
* **Not a caller's responsibility.** It is written by a ``post_delete``
  receiver on ``AUTH_USER_MODEL`` (connected in ``CommonDjangoConfig.ready``),
  so a deletion cannot happen *without* it — including deletions by cascade,
  by ``manage.py shell``, by the admin, and by a GDPR erasure job.
* **Not a separate key space by accident.** ``user_deleted:`` is distinct from
  ``jwt_blacklist:`` (per token) and ``user_blacklisted:`` (per user) so the
  three questions — is this token revoked, is this person banned, is this
  account gone — never answer each other's.

TTL
---
A tombstone shorter than the longest-lived credential that can name the dead
uid is a tombstone with a resurrection window at the end of it. So the default
is derived from the deployment's own ``JWT_REFRESH_TOKEN_LIFETIME`` rather
than a hardcoded number: lengthening the refresh TTL lengthens the tombstone
automatically. ``STAPEL_JWT_TOMBSTONE_TTL`` can raise it (keeping tombstones
longer is always safe); a value BELOW the refresh lifetime is a
misconfiguration and ``stapel_core.revocation.E002`` refuses the boot.

Store down
----------
Fails **CLOSED**: an unreachable store answers "tombstoned", so a deleted
principal is not admitted because the thing that knows they are deleted is
offline. This matches both blacklists, and it is the same reasoning — the
degraded state is exactly when a revocation matters most. It is also the more
expensive default, and deliberately so: it costs a consumer-mode service
availability while its cache is down, rather than costing a deleted person
their deletion. The single documented hatch ``STAPEL_BLACKLIST_FAIL_OPEN``
flips all three of these together; there is no separate knob, because two
knobs are how the halves of revocation drift apart.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager

from stapel_core.core.drop import DropReport, drop_cache_key

logger = logging.getLogger(__name__)

#: uids whose in-flight delete is a shadow-row re-key (see `shadow_rekey`).
_suppressed = threading.local()

#: Distinct key space from `jwt_blacklist:` (per token) and `user_blacklisted:`
#: (per user). Different questions, different keys.
TOMBSTONE_PREFIX = "user_deleted:"

#: What to compare when a lift reports ``NOT_FOUND`` (see ``core/drop.py``).
_MISS_HINT = (
    "check STAPEL_JWT_REVOCATION_NAMESPACE/_CACHE agree with the service that "
    "wrote the tombstone; a tombstone in another namespace is still enforced "
    "by the peers that read it"
)

#: Django's own default for JWT_REFRESH_TOKEN_LIFETIME, repeated here so the
#: TTL derivation does not silently read 0 from an unset setting.
DEFAULT_REFRESH_LIFETIME = 604800


def refresh_token_lifetime() -> int:
    """The longest-lived credential that can still name a deleted uid."""
    from django.conf import settings

    try:
        return int(
            getattr(settings, "JWT_REFRESH_TOKEN_LIFETIME", DEFAULT_REFRESH_LIFETIME)
        )
    except (TypeError, ValueError):
        return DEFAULT_REFRESH_LIFETIME


def tombstone_ttl() -> int:
    """How long a tombstone is kept, in seconds.

    Derived from the deployment's refresh lifetime unless it explicitly asks
    for longer. A shorter explicit value is not honoured silently — the
    system check refuses the boot — but this function still clamps, so a
    deployment running with the check silenced does not get the hole it
    silenced its way to.
    """
    from django.conf import settings

    floor = refresh_token_lifetime()
    configured = getattr(settings, "STAPEL_JWT_TOMBSTONE_TTL", None)
    if configured is None:
        return floor
    try:
        return max(int(configured), floor)
    except (TypeError, ValueError):
        return floor


def _key(uid) -> str:
    return f"{TOMBSTONE_PREFIX}{uid}"


def tombstone_user(uid) -> bool:
    """Record that *uid* is deleted. Returns False when the store refused it.

    A caller that ignores the result cannot tell a tombstone from a no-op —
    the same contract ``blacklist_user`` carries, for the same reason.
    """
    from stapel_core.core.revocation_store import revocation_cache

    try:
        revocation_cache().set(_key(uid), "1", tombstone_ttl())
    except Exception as exc:
        logger.error("Cannot write deletion tombstone for %s: %s", uid, exc)
        return False
    logger.info("Deletion tombstone written for user %s", uid)
    return True


def is_user_tombstoned(uid) -> bool:
    """Has this uid been deleted? Fails CLOSED (see the module docstring)."""
    from stapel_core.core.revocation_store import revocation_cache

    try:
        return bool(revocation_cache().get(_key(uid)))
    except Exception as exc:
        logger.error("Cannot read deletion tombstone for %s: %s", uid, exc)
        from .authentication import _blacklist_fail_open

        return not _blacklist_fail_open()


def lift_tombstone(uid) -> DropReport:
    """Remove a tombstone; reports what that actually did to the store.

    Exists for the operator who deleted the wrong row and restored it from a
    backup, and for tests. It is not part of any automatic path: nothing in
    this library lifts a tombstone on its own, because every automatic reason
    to do so would be a way for a token to undo a deletion again.

    **Until 0.47.0 this returned ``True`` for "the call did not raise"** — the
    same value whether the tombstone was lifted, was never written under the
    key this module computes (a peer on a different
    ``STAPEL_JWT_REVOCATION_NAMESPACE``, or a caller that reached for
    ``django.core.cache.cache``), or is still readable afterwards. That is the
    costliest instance of the shape 0.46.0 named in
    :mod:`stapel_core.verification.grants`, because of who is standing at the
    keyboard: an operator restoring a **wrongly deleted user**. A ``True`` that
    lifted nothing tells them the person is back while that person is still
    locked out of every consumer-mode service in the fleet, with nothing
    anywhere to say so.

    Now it measures — read, delete, read back — and the restore is checkable::

        report = lift_tombstone(uid)
        assert report, report.outcome     # NOT_FOUND / STILL_PRESENT / UNAVAILABLE

    ``NOT_FOUND`` is the one to read carefully here: it does NOT mean the user
    is admitted. It means nothing was tombstoned under this deployment's
    revocation namespace, which is either "there was nothing to lift" or "the
    tombstone is under a namespace this process does not read" — and
    :func:`is_user_tombstoned` in the service that HOLDS it will still refuse.
    """
    from stapel_core.core.revocation_store import revocation_cache, revocation_namespace

    return drop_cache_key(
        revocation_cache,
        _key(uid),
        what="deletion tombstone",
        namespace=revocation_namespace(),
        log=logger,
        hint=_MISS_HINT,
    )


@contextmanager
def shadow_rekey(uid):
    """A shadow row is being re-keyed — its delete is NOT a deletion.

    Consumer mode repairs a shadow row whose local primary key no longer
    matches the issuer id (a service database restored from before an id
    change) by deleting the row and creating it again under the id the token
    names. That delete is a local bookkeeping step: the account at the issuer
    is alive, and it is the account that is about to be re-created in the very
    next statement.

    Without this, ``_on_user_deleted`` cannot tell that repair from a real
    deletion, and writes a fleet-wide tombstone for a live person. The
    tombstone then outranks everything — ``get_or_create_user_from_jwt``
    consults it before any claim, in every service — so the repair locked the
    user out of the whole fleet until the tombstone's TTL (a week, by
    derivation from the refresh lifetime) ran out. Measured on a client
    fleet's stand, 2026-09-04.

    Deliberately keyed by uid and not a global flag: a cascade that reaches
    OTHER users' rows during the same block must still tombstone them. And
    deliberately thread-local — a second request deleting the same uid for
    real, concurrently, is not covered by this one's suppression.

    Reads as an operation, not a mode::

        with shadow_rekey(old_pk):
            User.objects.filter(pk=old_pk).delete()
    """
    key = str(uid)
    active = getattr(_suppressed, "uids", None)
    if active is None:
        active = set()
        _suppressed.uids = active
    already = key in active
    active.add(key)
    try:
        yield
    finally:
        if not already:
            active.discard(key)


def _is_shadow_rekey(uid) -> bool:
    return str(uid) in getattr(_suppressed, "uids", ())


def _on_user_deleted(sender, instance, **kwargs):
    """post_delete receiver — the deletion writes its own tombstone."""
    pk = getattr(instance, "pk", None)
    if pk is None:
        return
    if _is_shadow_rekey(pk):
        logger.info(
            "Deletion tombstone skipped for user %s: shadow-row re-key, not a "
            "deletion at the issuer",
            pk,
        )
        return
    tombstone_user(pk)


def connect_deletion_tombstone() -> None:
    """Wire the receiver onto AUTH_USER_MODEL. Idempotent (dispatch_uid)."""
    from django.conf import settings
    from django.db.models.signals import post_delete

    post_delete.connect(
        _on_user_deleted,
        sender=settings.AUTH_USER_MODEL,
        dispatch_uid="stapel_core_user_deletion_tombstone",
    )
