"""The identity MIRROR — the rows every service keeps about somebody else's users.

THE GAP THIS CLOSES
-------------------
A service that consumes an external identity keeps a local ``users.User`` row
per person it has ever seen: created from a JWT when
``JWT_CREATE_USERS_FROM_TOKEN`` is on, and from the ``user.created``
projection. That row carries email and username.

It belongs to no module. ``stapel_recordings`` owns recordings,
``stapel_profiles`` owns profiles, and each answers an erasure truthfully
about its own data — but nothing claims the mirror, because the mirror is not
any module's data. It is the framework's local copy of the identity, and the
framework never answered for it.

The consequence is the worst shape a compliance mechanism can have: **every
owner reports done and the receipt set is complete while the email survives.**
Measured end to end on a live fleet, 2026-09-16, on a purpose-built subject:

    owner        receipt   what actually remained in that store
    auth         0.25s     identity anonymised — correct
    profile      0.38s     profile row deleted — correct
    recordings   0.71s     Recording deleted … and users.User still held
                           'gdpr-drill@stapel.test', is_active=True

Nine parts done, request ``deleted``, ``completeness_waived=False``, and a
live email address in the store. Roughly 880 such rows existed across that
fleet at the time, claimed by nobody.

WHY IT LIVES IN CORE
--------------------
Because core creates the row. A fix in each consuming library would be seven
copies of one rule, and a service that adopted the mirror tomorrow would ship
the gap again — the defect was never that somebody forgot to declare an owner,
it was that declaring one was a thing you had to remember. Registration is
therefore automatic and keyed to the same setting that creates the rows: a
service inherits the obligation by mirroring users, not by remembering.

ANONYMISE, NEVER DELETE
-----------------------
Other tables reference these rows by id — a recording's owner, a membership, a
message author. Dropping the row takes unrelated rows with it under CASCADE,
or breaks them. So the row survives with every identifying field overwritten,
which is exactly what stapel-gdpr's ``erase_identity`` does to the primary row
in the identity-owning service.

**The two must not diverge**, or "erased" means one thing in auth and another
in every mirror. The field list and the tombstone shape here are the ones
``stapel_gdpr.lifecycle`` uses, and :func:`anonymize_identity` is written to be
the single implementation both call: stapel-gdpr delegates to it from the
release that depends on this one.

WHAT THIS DOES NOT TOUCH
------------------------
The identity-OWNING service. There ``JWT_CREATE_USERS_FROM_TOKEN`` is False —
auth mints tokens, it does not consume them — so this provider does not
register there and stapel-gdpr's ``erase_identity`` remains the only thing
that erases the primary row. One writer per row, in both kinds of service.
"""
from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)

#: The owner name this registers under, and the name a deployment must list in
#: ``STAPEL_GDPR["DATA_OWNERS"]`` for the orchestrator to ask it. Distinct from
#: ``auth`` on purpose: they are different rows in different databases, and a
#: receipt should say which one answered.
OWNER = "identity_mirror"

#: Subject types the mirror can erase. Only ``account`` — a mirror row is keyed
#: by user id and nothing else; claiming ``workspace`` or ``recording`` would
#: make the liveness probe a lie about what this can do.
SUBJECT_TYPES = ("account",)

#: Every field that can carry a person, overwritten in place. Mirrors
#: ``stapel_gdpr.lifecycle._IDENTITY_FIELDS`` exactly — see the module header
#: on why these two lists may not drift.
IDENTITY_FIELDS = (
    "email", "phone", "first_name", "last_name", "bio", "avatar",
    "oauth_provider", "oauth_id", "last_login_ip", "username",
)

#: The shape :func:`anonymize_identity` leaves behind. Idempotency is decided
#: by RECOGNISING it, not by checking whether the fields are empty — after an
#: anonymisation they are not empty, they hold the tombstone. Testing for
#: emptiness made a redelivery mint a SECOND tombstone for one person, which
#: is the history-splitting failure the guard was supposed to prevent.
TOMBSTONE_PREFIX = "deleted-"
TOMBSTONE_EMAIL_SUFFIX = "@deleted.invalid"

#: Fields whose surviving value proves the erasure did not happen. Checked
#: after the write, because an anonymiser that quietly does nothing is the
#: defect this whole module exists to close — success is measured, not assumed.
IDENTIFYING_FIELDS = ("email", "phone", "username")


def anonymize_identity(user) -> list[str]:
    """Overwrite every identity-bearing field on *user*, in place. Returns the
    field names changed.

    Irreversible on purpose: values are overwritten rather than moved, so
    nothing is left to reconstruct the person from. The primary key survives
    because references to it do.
    """
    tombstone = f"deleted-{uuid.uuid4().hex}"
    # RFC 2606 reserves .invalid — the address cannot be routed anywhere.
    updates = {"username": tombstone, "email": f"{tombstone}@deleted.invalid"}

    concrete = {f.name for f in user._meta.get_fields() if getattr(f, "concrete", False)}
    changed: list[str] = []
    for name in IDENTITY_FIELDS:
        if name not in concrete:
            continue
        if name in updates:
            value = updates[name]
        else:
            field = user._meta.get_field(name)
            value = None if field.null else ""
        setattr(user, name, value)
        changed.append(name)

    for name, value in (("is_active", False), ("is_staff", False), ("is_superuser", False)):
        if name in concrete:
            setattr(user, name, value)
            changed.append(name)
    if "staff_roles" in concrete:
        user.staff_roles = []
        changed.append("staff_roles")

    if hasattr(user, "set_unusable_password"):
        # Not a hash of an unknown string — a marker that can never validate.
        user.set_unusable_password()

    user.save()
    return changed


def is_tombstoned(user) -> bool:
    """Has this row already been through :func:`anonymize_identity`?"""
    username = str(getattr(user, "username", "") or "")
    email = str(getattr(user, "email", "") or "")
    return username.startswith(TOMBSTONE_PREFIX) or email.endswith(TOMBSTONE_EMAIL_SUFFIX)


def erase_subject(subject_type: str, subject_key: str, workspace_id=None):
    """The owner callable. Idempotent; counts what it changed.

    Returns ``None`` for a subject this does not claim, or for a user id this
    service has never mirrored — ``None`` receipts nothing, and an erasure the
    orchestrator is not waiting on is not ours to confirm.
    """
    if subject_type != "account":
        return None

    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.filter(pk=subject_key).first()
    if user is None:
        # Never mirrored here, or already dropped. Either way there is nothing
        # of this person in this store, which is the post-condition.
        return None

    if is_tombstoned(user):
        # A redelivery, not a second person. Report zero rather than minting a
        # fresh tombstone: delivery is at-least-once, and a second pseudonym
        # for one subject splits that person's history in two.
        return {"identity_mirror": 0}

    before = {
        name: getattr(user, name, None)
        for name in IDENTIFYING_FIELDS
        if getattr(user, name, None)
    }

    anonymize_identity(user)

    after = User.objects.filter(pk=subject_key).first()
    survived = [
        name for name, value in before.items()
        if after is not None and getattr(after, name, None) == value
    ]
    if survived:
        # Loud, and NOT a receipt: reporting done here is the exact failure
        # this module was written after.
        raise RuntimeError(
            "identity mirror not erased: %s still carries %s for %s"
            % (User._meta.label, ", ".join(sorted(survived)), subject_key)
        )

    logger.info("identity mirror anonymised [user=%s model=%s]", subject_key, User._meta.label)
    return {"identity_mirror": 1}


def reparent_on_merge(event) -> None:
    """``user.merged``: the losing account's mirror row stops naming anybody.

    Registering as a data owner subscribes ``user.deleted`` too, and core's
    own ``stapel_core.lifecycle.E001`` check refuses that in isolation, for a
    reason worth quoting: *a merge re-parents rows to the surviving account;
    an app that only knows deletion strands them.* The check caught this
    module the day it shipped.

    What a merge means here is narrower than for a module with its own
    tables. The mirror holds exactly one row per identity, keyed by user id,
    and after a merge ``from_user_id`` names an account that no longer exists
    anywhere — while local rows in OTHER modules are being re-parented to
    ``into_user_id`` by their own handlers, keyed off the event, not off this
    row.

    So there is nothing to re-parent and deleting would be wrong: other
    tables may still reference the losing id, and dropping it takes them with
    it under CASCADE or breaks them. The row is anonymised instead — the same
    treatment an erasure gives it, for the same reason. It keeps the key that
    other rows point at and stops carrying a person.

    Idempotent: a redelivered merge finds a tombstone and does nothing.
    """
    payload = getattr(event, "payload", None) or {}
    from_user_id = payload.get("from_user_id")
    if not from_user_id:
        logger.warning("user.merged without from_user_id: %r", payload)
        return

    from django.contrib.auth import get_user_model

    user = get_user_model().objects.filter(pk=from_user_id).first()
    if user is None or is_tombstoned(user):
        return
    anonymize_identity(user)
    logger.info(
        "identity mirror anonymised after a merge [from=%s into=%s]",
        from_user_id, payload.get("into_user_id"),
    )


def mirrors_identities() -> bool:
    """Does this process keep a local mirror of somebody else's users?

    The same setting that creates the rows decides who has to erase them, so
    the obligation cannot drift from the thing that incurs it.
    """
    from django.conf import settings

    return bool(getattr(settings, "JWT_CREATE_USERS_FROM_TOKEN", False))


def register_identity_mirror_owner() -> bool:
    """Register the mirror as a GDPR data owner, if this process has one.

    Called from core's own ``AppConfig.ready()``. Returns whether it
    registered, so a host can assert it rather than assume it.
    """
    if not mirrors_identities():
        return False
    try:
        from stapel_core.comm import on_action
        from stapel_core.gdpr import register_gdpr_owner

        register_gdpr_owner(OWNER, list(SUBJECT_TYPES), erase_subject)
        # Registering as an owner subscribes `user.deleted`, and an app that
        # knows deletion and not merge strands the merged account's rows —
        # stapel_core.lifecycle.E001 refuses that combination, correctly. See
        # reparent_on_merge for what a merge means to a mirror.
        on_action("user.merged")(reparent_on_merge)
    except Exception:  # pragma: no cover - never break a boot over this
        logger.warning("could not register the identity-mirror GDPR owner", exc_info=True)
        return False
    return True


__all__ = [
    "OWNER",
    "SUBJECT_TYPES",
    "IDENTITY_FIELDS",
    "IDENTIFYING_FIELDS",
    "anonymize_identity",
    "is_tombstoned",
    "TOMBSTONE_PREFIX",
    "TOMBSTONE_EMAIL_SUFFIX",
    "erase_subject",
    "reparent_on_merge",
    "mirrors_identities",
    "register_identity_mirror_owner",
]
