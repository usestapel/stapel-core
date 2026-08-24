"""Deactivation reaches the fleet — a live token cannot outrun `is_active=False`.

The defect
----------
Measured on a deployed fleet at 0.41.0. ``is_active=False`` at the issuer, same
unexpired access token, immediately after::

    iron-auth       /auth/api/v1/sessions/   -> 401   (issuer mode, correct)
    iron-profiles   /profiles/api/v1/me      -> 200   <-- still served
    iron-workspaces                          -> 200   <-- still served
    iron-billing                             -> 200   <-- still served

Two things were wrong, and either alone was enough.

**The claim satisfied the gate meant to judge it.** In consumer mode the
``is_active`` claim was replayed onto the local shadow row *before* the
``is_active`` gate ran. A token minted while the account was live carries
``is_active: true`` for the rest of its lifetime, so it reactivated the row and
then sailed through the check it had just satisfied. The ordering was
deliberate and documented — the reasoning being that the issuer is
authoritative — but a claim is not the issuer, it is a snapshot of the issuer
taken when the token was minted. A gate a credential can satisfy by asserting
its own conclusion is not a gate.

**And nothing told the peers.** Even with the ordering fixed, a consumer-mode
service learns lifecycle only from claims, so deactivation would still have
waited for the next mint — up to an access-token lifetime, or forever if the
user never signs in again.

The mechanism
-------------
Deactivation now publishes itself into the fleet-wide revocation namespace
(``stapel_core.core.revocation_store``, 0.39.0) under its own key space, and
consumer-mode verifiers consult it before trusting any claim — the same shape
as the deletion tombstone (0.40.0), for the same reason: the interim remedy was
``blacklist_user``, and "deactivate **and** remember to ban" is a procedure,
not a mechanism. A ``post_save`` receiver on ``AUTH_USER_MODEL`` makes the
state change carry its own announcement.

Reactivation DOES lift it — and that is the one place this deliberately
differs from the deletion tombstone
--------------------------------------------------------------------------
``lift_tombstone`` exists only for an operator who deleted the wrong row,
because every *automatic* reason to lift a tombstone is a way for a token to
undo a deletion. Deactivation is not deletion: suspending and restoring an
account is an ordinary, expected, repeatable operation, so the same receiver
that writes the key on ``is_active=False`` deletes it on ``is_active=True``.
**Do not copy the tombstone's no-automatic-lift rule here by analogy** — an
account that cannot be un-suspended is a bug, not a hardening.

What the local ``is_active`` column means now, in consumer mode
---------------------------------------------------------------
Nothing a token wrote. The claim no longer moves it in either direction, so a
shadow row's flag reflects only decisions that service made locally, and those
a bearer token can no longer override. Fleet-wide lifecycle lives in this key
space instead. In issuer mode nothing changes: the local database is the
account, and it always was.

Bulk updates
------------
``User.objects.filter(...).update(is_active=False)`` emits no signal — Django
does not send ``post_save`` for queryset updates — so it does not publish.
Call :func:`deactivate_user` alongside it, or deactivate through instances.
This is the one hole the receiver cannot close, and it is named here rather
than left to be discovered.

Store down
----------
Fails **CLOSED**, like both blacklists and the tombstone, through the same
single hatch ``STAPEL_BLACKLIST_FAIL_OPEN``: an unreachable store answers
"deactivated". The degraded state is exactly when a revocation matters most,
and two knobs are how the halves of revocation drift apart.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Distinct key space from `jwt_blacklist:` (per token), `user_blacklisted:`
#: (per user, moderation) and `user_deleted:` (the account is gone). Four
#: questions, four keys — none of them answering each other's.
DEACTIVATION_PREFIX = "user_deactivated:"


def deactivation_ttl() -> int:
    """How long a deactivation record is kept, in seconds.

    Shares :func:`~stapel_core.django.jwt.tombstone.tombstone_ttl` on purpose:
    both answer the same question — how long can a credential naming this uid
    still be presented — so they get one derivation and one knob
    (``STAPEL_JWT_TOMBSTONE_TTL``, floored at the refresh lifetime). A second
    setting here would be a second thing to get wrong, and the halves of
    revocation drift apart exactly along the seams where they were configured
    separately.

    Expiry is not a reactivation: it means every token minted before the
    deactivation is already dead, so the record has nothing left to refuse.
    A live account that is reactivated is lifted explicitly, not aged out.
    """
    from .tombstone import tombstone_ttl

    return tombstone_ttl()


def _key(uid) -> str:
    return f"{DEACTIVATION_PREFIX}{uid}"


def deactivate_user(uid) -> bool:
    """Publish "this uid is deactivated". False when the store refused it.

    Normally called for you by the ``post_save`` receiver. Call it directly
    only alongside a queryset ``.update(is_active=False)``, which emits no
    signal (see the module docstring).
    """
    from stapel_core.core.revocation_store import revocation_cache

    try:
        revocation_cache().set(_key(uid), "1", deactivation_ttl())
    except Exception as exc:
        logger.error("Cannot publish deactivation for %s: %s", uid, exc)
        return False
    logger.info("Deactivation published for user %s", uid)
    return True


def lift_deactivation(uid) -> bool:
    """Publish "this uid is active again". False when the store refused it.

    Unlike :func:`~stapel_core.django.jwt.tombstone.lift_tombstone`, this IS
    on an automatic path: the receiver calls it whenever a user is saved
    active. Reactivation is a legitimate operation and has to work without an
    operator remembering anything.
    """
    from stapel_core.core.revocation_store import revocation_cache

    try:
        revocation_cache().delete(_key(uid))
    except Exception as exc:
        logger.error("Cannot lift deactivation for %s: %s", uid, exc)
        return False
    return True


def is_user_deactivated(uid) -> bool:
    """Is this uid deactivated at the issuer? Fails CLOSED (see docstring)."""
    from stapel_core.core.revocation_store import revocation_cache

    try:
        return bool(revocation_cache().get(_key(uid)))
    except Exception as exc:
        logger.error("Cannot read deactivation for %s: %s", uid, exc)
        from .authentication import _blacklist_fail_open

        return not _blacklist_fail_open()


def _on_user_saved(sender, instance, created=False, update_fields=None, **kwargs):
    """post_save receiver — the lifecycle change carries its own announcement.

    Only the authoritative store publishes. A consumer-mode service must never
    write here: broadcasting its own shadow row's state would let a peer
    *lift* the issuer's deactivation, which is the failure this module exists
    to prevent, inverted.
    """
    from .utils import _create_users_from_token

    if _create_users_from_token():
        return

    pk = getattr(instance, "pk", None)
    if pk is None:
        return

    # A save that did not load the flag, or did not touch it, says nothing
    # about it — and the hot path (stamping last_login with
    # update_fields=['last_login']) goes through here on every single login.
    if "is_active" in (instance.get_deferred_fields() or ()):
        return
    if update_fields is not None and "is_active" not in update_fields:
        return

    if getattr(instance, "is_active", True):
        lift_deactivation(pk)
    else:
        deactivate_user(pk)


def connect_deactivation_broadcast() -> None:
    """Wire the receiver onto AUTH_USER_MODEL. Idempotent (dispatch_uid)."""
    from django.conf import settings
    from django.db.models.signals import post_save

    post_save.connect(
        _on_user_saved,
        sender=settings.AUTH_USER_MODEL,
        dispatch_uid="stapel_core_user_deactivation_broadcast",
    )
